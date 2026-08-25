"""
Bot de Música para Nerimity com Download Prévio de Áudio, Pacing de Precisão e WebRTC Sendonly.

CORREÇÃO APLICADA: o áudio ficava "acelerado e cortando" porque havia DOIS
temporizadores de tempo real disputando o ritmo da reprodução:
  1) O `aiortc.contrib.media.MediaPlayer`, que já pausa internamente para
     simular tempo real com base no `frame.time` do arquivo decodificado.
  2) O `ContinuousAudioTrack`, que aplicava OUTRO pacing por cima, baseado
     em `time.perf_counter()`.

Quando esses dois relógios saíam de sincronia (o que é praticamente garantido,
já que o tamanho de frame do MP3 raramente bate exatamente com os 960
amostras/20ms esperados), o bloco de "resync" do ContinuousAudioTrack
(`elif wait_time < -0.1: ...`) resetava o relógio de saída e liberava de uma
vez todo o áudio acumulado no buffer, sem respeitar o tempo real — daí o
efeito de "fast forward" e os cortes.

A solução foi remover o `MediaPlayer` (que faz seu próprio pacing) e usar um
decodificador dedicado (`AudioFileSource`) que apenas decodifica o arquivo
o mais rápido possível para uma fila com backpressure (sem tentar simular
tempo real). Assim, existe um ÚNICO responsável pelo ritmo da reprodução:
o `ContinuousAudioTrack`.
"""

import asyncio
import fractions
import os
import re
import tempfile
import threading
import time
from typing import Dict, Optional, List

import av
import numpy as np
import yt_dlp

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
)
from aiortc.sdp import candidate_from_sdp

from nerimity_sdk import Bot

# --- CONFIGURAÇÕES ---
TOKEN = "SEU_TOKEN_AQUI"

COMANDO_CONFIG = "!config"
COMANDO_SAIR = "!sair"
COMANDO_MESTRE = "!master"
SENHA_MESTRE = "SUA_SENHA_AQUI"

USUARIOS_BLOQUEADOS = set()

PEER_CONNECT_TIMEOUT = 20

# Limite máximo de músicas que podem ser adicionadas de uma vez via !play/!playlist
LIMITE_PLAYLIST = 50

# Pasta temporária para armazenamento local do áudio
TEMP_DIR = os.path.join(tempfile.gettempdir(), "kmusic_cache")
os.makedirs(TEMP_DIR, exist_ok=True)

# Configurações do yt-dlp para download local prévio
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
    'noplaylist': True,
    'nocheckcertificate': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

# Servidores STUN/TURN da plataforma
ICE_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="stun:stun.relay.metered.ca:80"),
    RTCIceServer(
        urls="turn:a.relay.metered.ca:80",
        username="b9fafdffb3c428131bd9ae10",
        credential="DTk2mXfXv4kJYPvD",
    ),
    RTCIceServer(
        urls="turn:a.relay.metered.ca:443",
        username="b9fafdffb3c428131bd9ae10",
        credential="DTk2mXfXv4kJYPvD",
    ),
]

bot = Bot(token=TOKEN)


def extrair_links_da_lista(texto: str) -> List[str]:
    """
    Extrai, em ordem, todos os links de uma mensagem de playlist personalizada
    (um link por linha, ou vários separados por espaço/vírgula). Linhas que
    não são links (títulos, comentários, numeração "1.", etc.) são ignoradas.
    """
    candidatos = re.split(r'[\s,]+', texto.strip())
    return [c for c in candidatos if c.startswith(('http://', 'https://'))]


class AudioFileSource:
    """
    Decodifica um arquivo (ou stream) de áudio em uma thread separada,
    SEM tentar simular tempo real, e entrega os frames já resampleados
    (s16, mono, 48kHz) através de uma fila assíncrona.

    A fila tem tamanho máximo (`buffer_frames`), o que cria backpressure:
    a thread de decodificação só produz mais rápido que o consumo até
    encher o buffer, depois disso ela espera. Isso evita tanto o consumo
    de memória descontrolado quanto qualquer tentativa paralela de
    "temporizar" o áudio — quem dita o ritmo é exclusivamente quem
    consome (`ContinuousAudioTrack`).
    """

    def __init__(self, path: str, loop: asyncio.AbstractEventLoop, buffer_frames: int = 200):
        self.path = path
        self.loop = loop
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=buffer_frames)
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        self._stopped = False
        self._finished = False
        self._future = loop.run_in_executor(None, self._run)

    def stop(self) -> None:
        self._stopped = True

    def _run(self) -> None:
        container = None
        try:
            container = av.open(self.path)
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                return

            for frame in container.decode(stream):
                if self._stopped:
                    break
                try:
                    resampled_frames = self._resampler.resample(frame)
                except Exception:
                    continue

                for r_frame in resampled_frames:
                    if self._stopped:
                        break
                    fut = asyncio.run_coroutine_threadsafe(self.queue.put(r_frame), self.loop)
                    fut.result()  # bloqueia a thread de decodificação (backpressure)

                if self._stopped:
                    break
        except Exception as e:
            print(f"Erro ao decodificar áudio ({self.path}): {e}")
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(self.queue.put(None), self.loop)
            except Exception:
                pass

    async def recv(self):
        if self._finished:
            return None
        frame = await self.queue.get()
        if frame is None:
            self._finished = True
            return None
        return frame


class ContinuousAudioTrack(MediaStreamTrack):
    """
    Faixa de áudio "burra": não decodifica nem paceia nada por conta
    própria. Ela só repassa ao WebRTC os frames que o "pump" central da
    VoiceSession (`_audio_pump_loop`) distribui para todos os ouvintes
    ao mesmo tempo.

    IMPORTANTE: antes, cada ouvinte tinha seu próprio relógio e todos
    chamavam `.recv()` diretamente na mesma fonte de áudio compartilhada
    — ou seja, com 2+ ouvintes no canal, cada um "roubava" frames do
    outro, recebendo só uma fração do áudio real enquanto o contador de
    tempo (pts) de cada um continuava avançando normalmente. O resultado
    era música tocando mais rápido que o normal e cortada. Agora existe
    um único pacer para a sessão inteira, e cada ouvinte só recebe uma
    cópia dos mesmos frames, na mesma cadência.
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        # Fila pequena: se este ouvinte específico atrasar para consumir
        # (rede lenta, etc.), preferimos descartar o frame mais antigo a
        # deixar a fila crescer e a reprodução dele atrasar cada vez mais.
        self._queue: "asyncio.Queue" = asyncio.Queue(maxsize=3)

    def push_frame(self, frame) -> None:
        """Chamado pelo pump central para entregar o próximo frame de 20ms."""
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass

    async def recv(self):
        return await self._queue.get()


class VoiceSession:
    """Gerencia conexões WebRTC em modo exclusivo de transmissão (Sendonly)."""

    def __init__(self, bot: Bot, channel_id: str):
        self.bot = bot
        self.channel_id = channel_id
        self.my_user_id: Optional[str] = None
        self.peers: Dict[str, RTCPeerConnection] = {}
        self.tracks: Dict[str, ContinuousAudioTrack] = {}
        self._connected = False

        self.fila: List[str] = []
        self.current_track: Optional[AudioFileSource] = None
        self.current_file_path: Optional[str] = None
        # ID do canal de TEXTO configurado, para poder avisar sobre o que
        # está tocando/fila. Setado pela GerenciadorSalas logo após a
        # criação da sessão.
        self.canal_texto_id: Optional[str] = None
        # Evita repetir o aviso de "fila acabou" a cada segundo enquanto
        # o bot fica ocioso esperando novas músicas, e evita mandar esse
        # aviso logo ao entrar no canal, antes de tocar qualquer coisa.
        self._avisou_fila_vazia = False
        self._ja_tocou_alguma_musica = False
        # Sinaliza (para a thread de download do yt-dlp) que o download
        # em andamento deve ser cancelado — setado por !stop/!sair.
        self._cancelar_evento = threading.Event()

        # --- Pipeline de pré-download ---
        # `fila` guarda só os links ainda NÃO iniciados. O `_prepare_loop`
        # roda em paralelo ao `_play_loop` e vai baixando, com antecedência,
        # a(s) próxima(s) música(s) (uma de cada vez, `maxsize=1`), deixando
        # o resultado pronto em `_prontos`. Assim, quando a música atual
        # termina, a próxima já está baixada e toca sem espera.
        self._prontos: "asyncio.Queue" = asyncio.Queue(maxsize=1)
        # Contador incrementado a cada `!stop`, usado para descartar
        # downloads que estavam em andamento antes do stop (e que só
        # terminariam DEPOIS dele) — evita tocar uma música "fantasma".
        self._geracao = 0

        self._play_task = asyncio.create_task(self._play_loop())
        self._prepare_task = asyncio.create_task(self._prepare_loop())
        # Único responsável por decidir QUANDO cada frame de 20ms sai —
        # compartilhado por todos os ouvintes desta sessão de voz.
        self._pump_task = asyncio.create_task(self._audio_pump_loop())

    async def join(self) -> None:
        gateway = self.bot._gateway
        socket_id = gateway.socket_id
        if not socket_id:
            raise RuntimeError("Gateway ainda não conectado.")

        await self.bot.rest.join_voice(self.channel_id, socket_id)
        self._connected = True

    async def leave(self) -> None:
        self.parar()
        self._play_task.cancel()
        self._prepare_task.cancel()
        self._pump_task.cancel()
        for pc in list(self.peers.values()):
            await pc.close()
        self.peers.clear()
        self.tracks.clear()
        if self._connected:
            await self.bot.rest.leave_voice(self.channel_id)
            self._connected = False

    def adicionar_musica(self, url: str):
        self.fila.append(url)

    def finalizar_musica_atual(self):
        # NÃO cancela downloads em andamento aqui: quem está sendo baixado
        # nesse momento é a PRÓXIMA música (pipeline de pré-download), não
        # a atual — ela já foi baixada antes de começar a tocar. Cancelar
        # esse download interromperia a preparação da próxima faixa.
        if self.current_track:
            try:
                self.current_track.stop()
            except Exception:
                pass
        self.current_track = None

        # Exclui o arquivo temporário baixado após o término da música
        if self.current_file_path and os.path.exists(self.current_file_path):
            try:
                os.remove(self.current_file_path)
            except Exception:
                pass
            self.current_file_path = None

    def pular_musica(self):
        # Só encerra a música atual. A próxima (se já estiver pronta em
        # `_prontos`, graças ao pré-download) assume imediatamente no
        # `_play_loop`, sem esperar novo download.
        self.finalizar_musica_atual()

    def _esvaziar_prontos(self) -> None:
        """Descarta tudo que já foi pré-baixado e apaga os arquivos temporários."""
        while True:
            try:
                item = self._prontos.get_nowait()
            except asyncio.QueueEmpty:
                break
            caminho = item.get("caminho")
            if item.get("temporario") and caminho and os.path.exists(caminho):
                try:
                    os.remove(caminho)
                except Exception:
                    pass

    def parar(self):
        self.fila.clear()
        # Invalida qualquer download em andamento no `_prepare_loop`: ao
        # terminar, ele vai perceber que a "geração" mudou e descartar o
        # resultado em vez de colocá-lo em `_prontos`.
        self._geracao += 1
        # Cancela o download que estiver rolando agora (o hook de
        # progresso do yt-dlp checa esta flag e aborta o download).
        self._cancelar_evento.set()
        self._esvaziar_prontos()
        self.finalizar_musica_atual()

    def _limpar_arquivo_parcial(self, caminho: Optional[str]) -> None:
        """Remove arquivos parciais deixados por um download cancelado no meio."""
        if not caminho:
            return
        for candidato in (caminho, caminho + ".part", caminho + ".ytdl"):
            try:
                if os.path.exists(candidato):
                    os.remove(candidato)
            except Exception:
                pass

    async def _enviar_texto(self, texto: str) -> None:
        """Envia um aviso no canal de texto configurado (se houver)."""
        if not self.canal_texto_id:
            return
        try:
            await self.bot.rest.create_message(self.canal_texto_id, texto)
        except Exception as e:
            print(f"Aviso: não foi possível enviar mensagem no canal de texto: {e}")

    async def _baixar_audio_local(self, url: str):
        """Baixa o arquivo de áudio completo antes de iniciar a reprodução."""
        loop = asyncio.get_event_loop()
        self._cancelar_evento.clear()
        arquivo_parcial = {"caminho": None}

        def _hook(d):
            if d.get("filename"):
                arquivo_parcial["caminho"] = d.get("filename")
            if self._cancelar_evento.is_set():
                # Qualquer exceção levantada dentro do hook interrompe o
                # download do yt-dlp imediatamente.
                raise Exception("__cancelado_pelo_usuario__")

        def _download():
            opcoes = dict(YTDL_OPTIONS)
            opcoes['progress_hooks'] = [_hook]
            with yt_dlp.YoutubeDL(opcoes) as ydl:
                info = ydl.extract_info(url, download=True)
                if 'entries' in info:
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(filename)
                mp3_filename = base + ".mp3"

                caminho_final = mp3_filename if os.path.exists(mp3_filename) else filename
                return caminho_final, info.get('title', 'Música Desconhecida')

        try:
            return await loop.run_in_executor(None, _download)
        except Exception as e:
            if self._cancelar_evento.is_set():
                print(f"⏹️ Download cancelado pelo usuário ({url}).")
            else:
                print(f"Erro ao baixar áudio ({url}): {e}")
            self._limpar_arquivo_parcial(arquivo_parcial["caminho"])
            return None, None

    async def _extrair_info(self, url: str):
        """
        Consulta o yt-dlp SEM baixar nada, só para descobrir os metadados
        do link (título, duração, se é transmissão ao vivo, e a URL direta
        do stream de áudio).
        """
        loop = asyncio.get_event_loop()

        def _extrair():
            opcoes = dict(YTDL_OPTIONS)
            opcoes['skip_download'] = True
            opcoes.pop('postprocessors', None)
            with yt_dlp.YoutubeDL(opcoes) as ydl:
                info = ydl.extract_info(url, download=False)
                if 'entries' in info:
                    info = info['entries'][0]
                return info

        try:
            return await loop.run_in_executor(None, _extrair)
        except Exception as e:
            print(f"Erro ao extrair informações ({url}): {e}")
            return None

    # Extensões de arquivo de áudio direto: tocamos via streaming (sem
    # baixar primeiro) para começar a tocar mais rápido — o av.open lê
    # direto da URL remota, igual faz com os links de rádio.
    EXTENSOES_ARQUIVO_FINITO = ('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.opus', '.wma')

    async def _preparar_musica(self, link_usuario: str) -> Optional[dict]:
        """
        Resolve um link (extrai metadados e, se necessário, baixa o
        áudio localmente) e devolve um dicionário pronto para tocar, ou
        `None` se falhar. Não mexe em `self.current_track` — só prepara.
        """
        url_sem_query = link_usuario.split('?')[0].lower()
        titulo_stream = None

        if url_sem_query.endswith(self.EXTENSOES_ARQUIVO_FINITO):
            # Link direto para um arquivo de áudio: streama sem baixar.
            info = None
            eh_ao_vivo = True
            titulo_stream = os.path.basename(url_sem_query) or "Arquivo de Áudio"
        else:
            info = await self._extrair_info(link_usuario)

            if info is not None:
                # Se o yt-dlp não retorna duração, ou marca como
                # live_status, é uma transmissão contínua/infinita
                # (rádio) — não pode (nem faz sentido) ser baixada.
                eh_ao_vivo = (
                    bool(info.get('is_live'))
                    or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live')
                    or not info.get('duration')
                )
            else:
                # yt-dlp não conseguiu extrair metadados: provavelmente
                # é uma URL de stream de áudio puro sem suporte
                # específico. Cai na heurística por extensão como
                # último recurso.
                eh_ao_vivo = (
                    link_usuario.endswith(('.m3u', '.m3u8', '.pls', '.aac'))
                    or ":80" in link_usuario
                )

        if eh_ao_vivo:
            caminho_ou_url = (info.get('url') if info else None) or link_usuario
            titulo = (info.get('title') if info else None) or titulo_stream or "Rádio Ao Vivo"
            eh_temporario = False
        else:
            caminho_ou_url, titulo = await self._baixar_audio_local(link_usuario)
            eh_temporario = True

        if not caminho_ou_url:
            return None

        return {"caminho": caminho_ou_url, "titulo": titulo, "temporario": eh_temporario}

    async def _prepare_loop(self):
        """
        Roda em paralelo ao `_play_loop`, baixando com antecedência a
        próxima música da fila (uma de cada vez — `_prontos` tem
        `maxsize=1`). Enquanto a música 1 está tocando, este loop já
        baixa a música 2 e a deixa pronta; assim que a 1 termina, a 2
        toca na hora, sem espera de download.
        """
        while True:
            if not self.fila or self._prontos.full():
                await asyncio.sleep(0.5)
                continue

            link_usuario = self.fila.pop(0)
            geracao_no_inicio = self._geracao

            item = await self._preparar_musica(link_usuario)

            if item is None:
                continue

            if self._geracao != geracao_no_inicio:
                # Houve um `!stop` enquanto isso baixava: descarta.
                caminho = item.get("caminho")
                if item.get("temporario") and caminho and os.path.exists(caminho):
                    try:
                        os.remove(caminho)
                    except Exception:
                        pass
                continue

            await self._prontos.put(item)

    async def _play_loop(self):
        """
        Loop principal de reprodução: só consome itens já preparados
        (baixados) por `_prepare_loop` — não baixa nada diretamente.
        """
        while True:
            if self.current_track is not None:
                await asyncio.sleep(0.5)
                continue

            if self._prontos.empty():
                if (
                    not self.fila
                    and self._ja_tocou_alguma_musica
                    and not self._avisou_fila_vazia
                ):
                    self._avisou_fila_vazia = True
                    await self._enviar_texto("🏁 A fila acabou! Manda mais música com `!play`.")
                await asyncio.sleep(0.3)
                continue

            self._avisou_fila_vazia = False
            item = await self._prontos.get()
            caminho_ou_url = item["caminho"]
            titulo = item["titulo"]
            eh_temporario = item["temporario"]

            try:
                loop = asyncio.get_running_loop()
                self.current_track = AudioFileSource(caminho_ou_url, loop)
                if eh_temporario:
                    self.current_file_path = caminho_ou_url
                self._ja_tocou_alguma_musica = True
                print(f"▶️ Tocando: {titulo}")
                await self._enviar_texto(f"▶️ Tocando agora: **{titulo}**")
            except Exception as e:
                print(f"Erro ao carregar o arquivo de áudio: {e}")
                self.finalizar_musica_atual()

    async def _audio_pump_loop(self):
        """
        Único temporizador de tempo real da sessão. Lê da fonte de áudio
        atual (`self.current_track`), monta frames de exatamente 960
        amostras (20ms a 48kHz) no ritmo correto via `perf_counter`, e
        distribui (broadcast) cada frame para TODOS os ouvintes conectados
        no momento. Isso garante que todo mundo escute exatamente a mesma
        coisa, no mesmo instante, sem disputar a mesma fonte.
        """
        FRAME_SIZE = 960
        fifo = av.AudioFifo()
        pts = 0
        start_time = time.perf_counter()

        while True:
            now = time.perf_counter()
            target_time = start_time + (pts / 48000.0)
            wait_time = target_time - now

            if wait_time > 0:
                await asyncio.sleep(wait_time)
            elif wait_time < -0.1:
                # Atraso real e grande (hiccup de CPU/IO) — ressincroniza
                # sem tentar "correr atrás", evitando bursts.
                start_time = time.perf_counter() - (pts / 48000.0)

            while fifo.samples < FRAME_SIZE:
                frame = None
                if self.current_track:
                    try:
                        frame = await self.current_track.recv()
                    except Exception:
                        self.finalizar_musica_atual()

                if frame is not None:
                    try:
                        fifo.write(frame)
                    except Exception:
                        pass
                else:
                    if self.current_track is not None:
                        # Fim do arquivo/stream: avança para a próxima
                        # música da fila.
                        self.finalizar_musica_atual()
                    silence_data = np.zeros((1, FRAME_SIZE), dtype=np.int16)
                    silence_frame = av.AudioFrame.from_ndarray(silence_data, format="s16", layout="mono")
                    silence_frame.sample_rate = 48000
                    fifo.write(silence_frame)

            if fifo.samples >= FRAME_SIZE:
                out_frame = fifo.read(FRAME_SIZE)
            else:
                silence_data = np.zeros((1, FRAME_SIZE), dtype=np.int16)
                out_frame = av.AudioFrame.from_ndarray(silence_data, format="s16", layout="mono")
                out_frame.sample_rate = 48000

            out_frame.pts = pts
            out_frame.time_base = fractions.Fraction(1, 48000)
            pts += FRAME_SIZE

            # Distribui o MESMO frame para todos os ouvintes conectados.
            for track in list(self.tracks.values()):
                track.push_frame(out_frame)

    # -- WEBRTC SENDONLY --
    def _nova_conexao(self, user_id: str) -> RTCPeerConnection:
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ICE_SERVERS))
        track = ContinuousAudioTrack()

        transceiver = pc.addTransceiver("audio", direction="sendonly")
        transceiver.sender.replaceTrack(track)

        self.peers[user_id] = pc
        self.tracks[user_id] = track

        @pc.on("connectionstatechange")
        async def on_state_change():
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self._remover_conexao(user_id)

        asyncio.create_task(self._watchdog_conexao(user_id, pc))
        return pc

    async def _watchdog_conexao(self, user_id: str, pc: RTCPeerConnection) -> None:
        await asyncio.sleep(PEER_CONNECT_TIMEOUT)
        if self.peers.get(user_id) is pc and pc.connectionState not in ("connected",):
            await self._remover_conexao(user_id)

    async def _remover_conexao(self, user_id: str) -> None:
        pc = self.peers.pop(user_id, None)
        self.tracks.pop(user_id, None)
        if pc:
            await pc.close()

    async def _enviar_sinal(self, to_user_id: str, signal: dict) -> None:
        await self.bot._gateway.emit("voice:signal_send", {"channelId": self.channel_id, "toUserId": to_user_id, "signal": signal})

    async def ao_usuario_entrar(self, payload: dict) -> None:
        if not self._connected: return
        user_id = payload.get("userId")
        if not user_id or user_id == self.my_user_id or user_id in self.peers: return
        pc = self._nova_conexao(user_id)
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._enviar_sinal(user_id, {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp})

    async def ao_usuario_sair(self, payload: dict) -> None:
        user_id = payload.get("userId")
        if user_id: await self._remover_conexao(user_id)

    async def ao_receber_sinal(self, payload: dict) -> None:
        try:
            if payload.get("channelId") != self.channel_id: return
            from_user_id = payload.get("fromUserId")
            signal = payload.get("signal") or {}
            if not from_user_id: return

            pc = self.peers.get(from_user_id)
            if "sdp" in signal:
                if pc is None: pc = self._nova_conexao(from_user_id)
                desc = RTCSessionDescription(sdp=signal["sdp"], type=signal["type"])
                await pc.setRemoteDescription(desc)
                if signal["type"] == "offer":
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    await self._enviar_sinal(from_user_id, {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp})
            elif signal.get("candidate"):
                if pc is None: return
                cand_info = signal["candidate"]
                cand_str = cand_info.get("candidate", "")
                if cand_str.startswith("candidate:"): cand_str = cand_str.split(":", 1)[1]
                if not cand_str: return
                candidate = candidate_from_sdp(cand_str)
                candidate.sdpMid = cand_info.get("sdpMid")
                candidate.sdpMLineIndex = cand_info.get("sdpMLineIndex")
                await pc.addIceCandidate(candidate)
        except Exception as e:
            print(f"Aviso de sinalização WebRTC: {e}")


class ConfigState:
    AGUARDANDO_MODELO = "aguardando_modelo"

class Sala:
    def __init__(self, canal_texto_id: str, canal_voz_id: str, voice_session: "VoiceSession"):
        self.canal_texto_id = canal_texto_id
        self.canal_voz_id = canal_voz_id
        self.voice_session = voice_session

class FluxoConfig:
    def __init__(self, dm_channel_id: str, user_id: str):
        self.dm_channel_id = dm_channel_id
        self.user_id = user_id
        self.estado = ConfigState.AGUARDANDO_MODELO

class GerenciadorSalas:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.my_user_id: Optional[str] = None
        self.salas_por_texto: Dict[str, Sala] = {}
        self.salas_por_voz: Dict[str, Sala] = {}
        self.fluxos: Dict[str, FluxoConfig] = {}

    async def _enviar_dm(self, channel_id: str, texto: str) -> None:
        if channel_id:
            await self.bot.rest.create_message(channel_id, texto)

    def sala_por_canal_texto(self, canal_texto_id: str) -> Optional[Sala]:
        return self.salas_por_texto.get(canal_texto_id)

    def sala_por_canal_voz(self, canal_voz_id: str) -> Optional[Sala]:
        return self.salas_por_voz.get(canal_voz_id)

    def _extrair_ids_do_modelo(self, texto: str):
        """Extrai 'ID TEXTO: ...' e 'ID VOZ: ...' de um texto preenchido pelo usuário."""
        match_texto = re.search(r'ID\s*TEXTO\s*:\s*(\S+)', texto, re.IGNORECASE)
        match_voz = re.search(r'ID\s*VOZ\s*:\s*(\S+)', texto, re.IGNORECASE)
        canal_texto_id = match_texto.group(1).strip() if match_texto else None
        canal_voz_id = match_voz.group(1).strip() if match_voz else None
        return canal_texto_id, canal_voz_id

    async def iniciar_config(self, dm_channel_id: str, user_id: str) -> None:
        self.fluxos[dm_channel_id] = FluxoConfig(dm_channel_id, user_id)
        await self._enviar_dm(
            dm_channel_id,
            "👋 Olá! Sou o Bot de Música do Nerimity.\n\n"
            "Preencha o modelo abaixo com os IDs e me envie de volta:\n\n"
            "```\nID TEXTO: \nID VOZ: \n```"
        )

    async def _concluir_configuracao(
        self, dm_channel_id: str, user_id: str, canal_texto_id: str, canal_voz_id: str
    ) -> None:
        voice_session = VoiceSession(self.bot, canal_voz_id)
        voice_session.my_user_id = self.my_user_id
        voice_session.canal_texto_id = canal_texto_id
        try:
            await voice_session.join()
        except Exception as exc:
            await self._enviar_dm(dm_channel_id, f"❌ Erro ao entrar no canal de voz: {exc}. Verifique o ID.")
            return

        sala = Sala(canal_texto_id, canal_voz_id, voice_session)
        self.salas_por_texto[canal_texto_id] = sala
        self.salas_por_voz[canal_voz_id] = sala
        self.fluxos.pop(dm_channel_id, None)

        await self._enviar_dm(
            dm_channel_id,
            "🎉 Pronto!\n\nNo canal de texto configurado, utilize:\n"
            "• `!play [URL/Link]` - Toca músicas, lives ou rádios\n"
            "• `!play` com vários links (um por linha) ou `!playlist` - Toca uma playlist personalizada, em ordem\n"
            "• `!skip` - Pula a música atual\n"
            "• `!fila` - Exibe as próximas músicas\n"
            "• `!stop` - Limpa a fila e desliga a música\n"
            "• `!sair` - Tira o bot do canal de voz"
        )

    async def processar_resposta_dm(self, dm_channel_id: str, texto: str) -> None:
        fluxo = self.fluxos.get(dm_channel_id)
        if not fluxo: return
        texto_original = texto or ""
        texto = texto_original.strip()
        if not texto: return

        if fluxo.estado == ConfigState.AGUARDANDO_MODELO:
            canal_texto_id, canal_voz_id = self._extrair_ids_do_modelo(texto_original)
            if not canal_texto_id or not canal_voz_id:
                await self._enviar_dm(
                    dm_channel_id,
                    "⚠️ Não consegui encontrar os dois IDs preenchidos. Envie novamente no formato:\n\n"
                    "```\nID TEXTO: 123456\nID VOZ: 654321\n```"
                )
                return
            await self._concluir_configuracao(dm_channel_id, fluxo.user_id, canal_texto_id, canal_voz_id)

    async def sair_da_sala(self, dm_channel_id: str, canal_voz_id: str) -> None:
        sala = self.salas_por_voz.pop(canal_voz_id, None)
        if not sala: return
        self.salas_por_texto.pop(sala.canal_texto_id, None)
        await sala.voice_session.leave()
        await self._enviar_dm(dm_channel_id, f"🔇 O bot saiu do canal de voz `{canal_voz_id}`.")

    async def sair_da_sala_por_texto(self, canal_texto_id: str) -> None:
        """Usado quando o comando de sair vem do próprio canal de texto configurado."""
        sala = self.salas_por_texto.pop(canal_texto_id, None)
        if not sala: return
        self.salas_por_voz.pop(sala.canal_voz_id, None)
        await sala.voice_session.leave()
        await self.bot.rest.create_message(canal_texto_id, "🔇 Saindo do canal de voz. Até mais!")

    async def resetar_tudo(self, dm_channel_id: str) -> None:
        for sala in list(self.salas_por_voz.values()):
            await sala.voice_session.leave()
        self.salas_por_texto.clear()
        self.salas_por_voz.clear()
        self.fluxos.pop(dm_channel_id, None)
        await self._enviar_dm(dm_channel_id, "🔄 Bot desconectado de todas as chamadas.")


gerenciador = GerenciadorSalas(bot)


@bot.on("ready")
async def on_ready(me):
    gerenciador.my_user_id = getattr(me, "id", None)
    print(f"✅ Conectado com sucesso como {getattr(me, 'username', '?')}")

@bot.on("voice:user_joined")
async def on_voice_user_joined(payload):
    sala = gerenciador.sala_por_canal_voz(str(payload.get("channelId", "")))
    if sala: await sala.voice_session.ao_usuario_entrar(payload)

@bot.on("voice:user_left")
async def on_voice_user_left(payload):
    sala = gerenciador.sala_por_canal_voz(str(payload.get("channelId", "")))
    if sala: await sala.voice_session.ao_usuario_sair(payload)

@bot.on("voice:signal_received")
async def on_voice_signal(payload):
    sala = gerenciador.sala_por_canal_voz(str(payload.get("channelId", "")))
    if sala: await sala.voice_session.ao_receber_sinal(payload)

@bot.on("message:created")
async def on_message(event):
    msg = event.message
    channel_id = str(msg.channel_id)
    conteudo = getattr(msg, "content", "") or ""

    autor_id = getattr(msg.created_by, "id", None) if hasattr(msg, "created_by") else None
    if gerenciador.my_user_id and str(autor_id) == str(gerenciador.my_user_id):
        return

    eh_dm = not getattr(msg, "server_id", None)
    partes = conteudo.strip().split(maxsplit=1)
    comando = partes[0].lower() if partes else ""
    argumento = partes[1].strip() if len(partes) > 1 else ""

    # -- DM --
    if eh_dm:
        if comando == COMANDO_MESTRE and argumento == SENHA_MESTRE:
            await gerenciador.resetar_tudo(channel_id)
            return

        if str(autor_id) in USUARIOS_BLOQUEADOS: return

        if comando == COMANDO_SAIR:
            await gerenciador.sair_da_sala(channel_id, argumento)
            return

        if comando == COMANDO_CONFIG:
            await gerenciador.iniciar_config(channel_id, autor_id)
            return

        # Se a mensagem já vem com os dois IDs preenchidos (o modelo
        # "ID TEXTO: ... / ID VOZ: ..."), configura direto, mesmo que a
        # pessoa não tenha pedido !config antes nem esteja num fluxo ativo.
        canal_texto_id, canal_voz_id = gerenciador._extrair_ids_do_modelo(conteudo)
        if canal_texto_id and canal_voz_id:
            gerenciador.fluxos.pop(channel_id, None)
            await gerenciador._concluir_configuracao(channel_id, autor_id, canal_texto_id, canal_voz_id)
            return

        if channel_id in gerenciador.fluxos:
            await gerenciador.processar_resposta_dm(channel_id, conteudo)
            return

        # Qualquer outra mensagem em DM já inicia a configuração
        # automaticamente — não precisa mais digitar !config.
        await gerenciador.iniciar_config(channel_id, autor_id)
        return

    # -- SERVIDOR --
    sala = gerenciador.sala_por_canal_texto(channel_id)
    if sala:
        if comando in ["!play", "!tocar", "!playlist", "!lista"]:
            if argumento:
                links = extrair_links_da_lista(argumento)
                if not links:
                    # Não veio nenhum link reconhecível (ex: começa sem
                    # "http") — trata a mensagem inteira como um único
                    # pedido (ex: busca por nome de música).
                    links = [argumento.strip()]

                cortado = len(links) > LIMITE_PLAYLIST
                if cortado:
                    links = links[:LIMITE_PLAYLIST]

                for link in links:
                    sala.voice_session.adicionar_musica(link)

                if len(links) > 1:
                    aviso_corte = f" (limite de {LIMITE_PLAYLIST} por vez, o restante foi ignorado)" if cortado else ""
                    await bot.rest.create_message(
                        channel_id,
                        f"🎶 {len(links)} músicas adicionadas à fila, na ordem enviada!{aviso_corte} "
                        "Vai começar a tocar em instantes, aguarde um pouco 🙂"
                    )
                else:
                    await bot.rest.create_message(
                        channel_id,
                        "🎶 Adicionado à fila! Vai começar a tocar em instantes, aguarde um pouco 🙂"
                    )
            else:
                await bot.rest.create_message(
                    channel_id,
                    "⚠️ Informe um link válido (YouTube, SoundCloud, Rádio ou Arquivo de Áudio).\n"
                    "Dica: para tocar uma playlist personalizada, mande vários links, um por linha, depois de `!play`."
                )

        elif comando in ["!skip", "!pular"]:
            sala.voice_session.pular_musica()
            await bot.rest.create_message(channel_id, "⏭️ Faixa pulada.")

        elif comando in ["!stop", "!parar"]:
            sala.voice_session.parar()
            await bot.rest.create_message(channel_id, "🛑 Reprodução interrompida e fila esvaziada.")

        elif comando == "!sair" and not argumento:
            await gerenciador.sair_da_sala_por_texto(channel_id)

        elif comando in ["!fila", "!queue"]:
            fila = sala.voice_session.fila
            if not fila:
                await bot.rest.create_message(channel_id, "A fila está vazia no momento.")
            else:
                lista = "\n".join(f"{i+1}. {url}" for i, url in enumerate(fila[:10]))
                await bot.rest.create_message(channel_id, f"📋 **Fila Atual:**\n{lista}")


if __name__ == "__main__":
    bot.run()
