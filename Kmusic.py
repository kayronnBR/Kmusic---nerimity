"""
Bot de Música Ultra Leve para Nerimity.
Focado em Streaming Direto, Rádios, Google Drive (Arquivos e Pastas) e Dropbox.
Possui comando !extrair na DM para gerar arquivos .txt com listas de links.
"""

import asyncio
import fractions
import gc
import io
import re
import time
import urllib.request
from typing import Dict, Optional, List

import av
import numpy as np

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
TOKEN = "COLOQUE-TOKEN-AQUI"

COMANDO_CONFIG = "!config"
COMANDO_SAIR = "!sair"
COMANDO_MESTRE = "!master"
SENHA_MESTRE = "SUA_SENHA_AQUI"

USUARIOS_BLOQUEADOS = set()

PEER_CONNECT_TIMEOUT = 15
LIMITE_PLAYLIST = 50  # Limite máximo de músicas tocadas por vez na fila

ICE_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="stun:stun.relay.metered.ca:80"),
    RTCIceServer(
        urls="turn:a.relay.metered.ca:80",
        username="b9fafdffb3c428131bd9ae10",
        credential="DTk2mXfXv4kJYPvD",
    ),
]

bot = Bot(token=TOKEN)


def _obter_html_gdrive(folder_id: str) -> str:
    """Faz a requisição HTTP leve para a pasta do Google Drive."""
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read().decode('utf-8', errors='ignore')


async def extrair_todos_links_gdrive(candidato: str) -> List[str]:
    """Varre uma pasta do Google Drive e devolve TODOS os links sem limite."""
    match_pasta = re.search(r'folders/([a-zA-Z0-9_-]+)', candidato)
    if not match_pasta:
        return []
    folder_id = match_pasta.group(1)
    try:
        html = await asyncio.to_thread(_obter_html_gdrive, folder_id)
        ids = re.findall(r'/file/d/([a-zA-Z0-9_-]+)', html)
        ids_unicos = list(dict.fromkeys(ids))
        return [f"https://drive.google.com/uc?export=download&id={fid}" for fid in ids_unicos]
    except Exception as e:
        print(f"Erro ao ler pasta do Drive: {e}")
        return []


async def extrair_links_da_lista(texto: str) -> List[str]:
    """Extrai e converte links individuais e pastas (para uso no !play)."""
    candidatos = re.split(r'[\s,]+', texto.strip())
    links_prontos = []
    
    for c in candidatos:
        if c.startswith(('http://', 'https://')):
            if "drive.google.com" in c and "folders/" in c:
                links_pasta = await extrair_todos_links_gdrive(c)
                links_prontos.extend(links_pasta)
            elif "drive.google.com/file/d/" in c:
                match = re.search(r'/d/([a-zA-Z0-9_-]+)', c)
                if match:
                    links_prontos.append(f"https://drive.google.com/uc?export=download&id={match.group(1)}")
            elif "dropbox.com" in c:
                c = c.replace("?dl=0", "?dl=1")
                if "&dl=1" not in c and "?dl=1" not in c:
                    c += "&dl=1" if "?" in c else "?dl=1"
                links_prontos.append(c)
            else:
                links_prontos.append(c)
            
    return links_prontos


class AudioFileSource:
    def __init__(self, url: str, loop: asyncio.AbstractEventLoop, buffer_frames: int = 50):
        self.url = url
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
            container = av.open(self.url, options={'timeout': '5000000'})
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
                    fut.result()

                if self._stopped:
                    break
        except Exception as e:
            print(f"Erro ao decodificar link: {e}")
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
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue: "asyncio.Queue" = asyncio.Queue(maxsize=2)

    def push_frame(self, frame) -> None:
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
    def __init__(self, bot: Bot, channel_id: str):
        self.bot = bot
        self.channel_id = channel_id
        self.my_user_id: Optional[str] = None
        self.peers: Dict[str, RTCPeerConnection] = {}
        self.tracks: Dict[str, ContinuousAudioTrack] = {}
        self._connected = False

        self.fila: List[str] = []
        self.current_track: Optional[AudioFileSource] = None
        self.canal_texto_id: Optional[str] = None
        self._avisou_fila_vazia = False
        self._ja_tocou_alguma_musica = False
        self._play_task = asyncio.create_task(self._play_loop())
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
        if self.current_track:
            try:
                self.current_track.stop()
            except Exception:
                pass
        self.current_track = None
        gc.collect()

    def pular_musica(self):
        self.finalizar_musica_atual()

    def parar(self):
        self.fila.clear()
        self.finalizar_musica_atual()

    async def _enviar_texto(self, texto: str) -> None:
        if not self.canal_texto_id:
            return
        try:
            await self.bot.rest.create_message(self.canal_texto_id, texto)
        except Exception:
            pass

    async def _play_loop(self):
        while True:
            if not self.fila or self.current_track is not None:
                if (
                    not self.fila
                    and self.current_track is None
                    and self._ja_tocou_alguma_musica
                    and not self._avisou_fila_vazia
                ):
                    self._avisou_fila_vazia = True
                    await self._enviar_texto("🏁 A fila acabou! Mande mais links ou pastas de áudio.")
                await asyncio.sleep(1)
                continue

            self._avisou_fila_vazia = False
            link_usuario = self.fila.pop(0)

            try:
                loop = asyncio.get_running_loop()
                self.current_track = AudioFileSource(link_usuario, loop)
                self._ja_tocou_alguma_musica = True
                await self._enviar_texto(f"▶️ Tentando sintonizar:\n`{link_usuario[:80]}...`")
            except Exception as e:
                print(f"Erro ao carregar áudio: {e}")
                self.finalizar_musica_atual()

    async def _audio_pump_loop(self):
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
        match_texto = re.search(r'ID\s*TEXTO\s*:\s*(\S+)', texto, re.IGNORECASE)
        match_voz = re.search(r'ID\s*VOZ\s*:\s*(\S+)', texto, re.IGNORECASE)
        canal_texto_id = match_texto.group(1).strip() if match_texto else None
        canal_voz_id = match_voz.group(1).strip() if match_voz else None
        return canal_texto_id, canal_voz_id

    async def iniciar_config(self, dm_channel_id: str, user_id: str) -> None:
        self.fluxos[dm_channel_id] = FluxoConfig(dm_channel_id, user_id)
        await self._enviar_dm(
            dm_channel_id,
            "👋 Olá! Sou o Bot de Música (Modo Leve).\n\n"
            "Preencha o modelo abaixo com os IDs e me envie de volta:\n\n"
            "```\nID TEXTO: \nID VOZ: \n```\n"
            "💡 *Dica:* Para organizar uma pasta grande do Drive, envie `!extrair <link_da_pasta>`."
        )

    async def processar_comando_extrair(self, dm_channel_id: str, argumento: str) -> None:
        """Processa a extração de pastas grandes e envia a lista formatada no chat."""
        if not argumento or "drive.google.com" not in argumento:
            await self._enviar_dm(
                dm_channel_id,
                "⚠️ Uso correto: `!extrair https://drive.google.com/drive/folders/SUA_PASTA`"
            )
            return

        await self._enviar_dm(dm_channel_id, "🔍 Varrendo pasta do Google Drive... Aguarde.")
        links = await extrair_todos_links_gdrive(argumento)

        if not links:
            await self._enviar_dm(dm_channel_id, "❌ Nenhuma música encontrada ou a pasta não é pública.")
            return

        total = len(links)
        await self._enviar_dm(
            dm_channel_id,
            f"✅ **{total} música(s) encontrada(s)!**\n"
            f"Abaixo estão os comandos `!play` prontos divididos em blocos para você copiar e usar no servidor:"
        )

        # Envia em blocos de 20 links para não estourar o limite de caracteres do chat
        tamanho_bloco = 20
        for i in range(0, total, tamanho_bloco):
            grupo = links[i:i + tamanho_bloco]
            num_bloco = (i // tamanho_bloco) + 1
            texto_bloco = f"**Bloco {num_bloco} (Músicas {i+1} até {min(i+tamanho_bloco, total)}):**\n"
            texto_bloco += f"```\n!play {' '.join(grupo)}\n```"
            await self._enviar_dm(dm_channel_id, texto_bloco)

    async def _concluir_configuracao(self, dm_channel_id: str, user_id: str, canal_texto_id: str, canal_voz_id: str) -> None:
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
            "• `!play [Links/Pastas]` - Reproduz áudios\n"
            "• `!skip` - Pula a faixa\n"
            "• `!fila` - Exibe a fila atual\n"
            "• `!stop` - Interrompe o som\n"
            "• `!sair` - Desconecta o bot"
            "• `!extair` - pega uma lista de link de cada música no Google drive"
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
                await self._enviar_dm(dm_channel_id, "⚠️ IDs inválidos. Envie no formato:\n```\nID TEXTO: 123\nID VOZ: 456\n```")
                return
            await self._concluir_configuracao(dm_channel_id, fluxo.user_id, canal_texto_id, canal_voz_id)

    async def sair_da_sala(self, dm_channel_id: str, canal_voz_id: str) -> None:
        sala = self.salas_por_voz.pop(canal_voz_id, None)
        if not sala: return
        self.salas_por_texto.pop(sala.canal_texto_id, None)
        await sala.voice_session.leave()
        await self._enviar_dm(dm_channel_id, f"🔇 Saiu do canal `{canal_voz_id}`.")

    async def sair_da_sala_por_texto(self, canal_texto_id: str) -> None:
        sala = self.salas_por_texto.pop(canal_texto_id, None)
        if not sala: return
        self.salas_por_voz.pop(sala.canal_voz_id, None)
        await sala.voice_session.leave()
        await self.bot.rest.create_message(canal_texto_id, "🔇 Saindo do canal de voz.")

    async def resetar_tudo(self, dm_channel_id: str) -> None:
        for sala in list(self.salas_por_voz.values()):
            await sala.voice_session.leave()
        self.salas_por_texto.clear()
        self.salas_por_voz.clear()
        self.fluxos.pop(dm_channel_id, None)
        await self._enviar_dm(dm_channel_id, "🔄 Bot desconectado de tudo.")


gerenciador = GerenciadorSalas(bot)


@bot.on("ready")
async def on_ready(me):
    gerenciador.my_user_id = getattr(me, "id", None)
    print(f"✅ Conectado com sucesso (Extrator de links ativado via DM!)")

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
        
        # COMANDO NOVO NA DM
        if comando in ["!extrair", "!links", "!pasta"]:
            await gerenciador.processar_comando_extrair(channel_id, argumento)
            return

        canal_texto_id, canal_voz_id = gerenciador._extrair_ids_do_modelo(conteudo)
        if canal_texto_id and canal_voz_id:
            gerenciador.fluxos.pop(channel_id, None)
            await gerenciador._concluir_configuracao(channel_id, autor_id, canal_texto_id, canal_voz_id)
            return

        if channel_id in gerenciador.fluxos:
            await gerenciador.processar_resposta_dm(channel_id, conteudo)
            return
        await gerenciador.iniciar_config(channel_id, autor_id)
        return

    # -- SERVIDOR --
    sala = gerenciador.sala_por_canal_texto(channel_id)
    if sala:
        if comando in ["!play", "!tocar", "!playlist"]:
            if argumento:
                links = await extrair_links_da_lista(argumento)
                if not links:
                    links = [argumento.strip()]

                cortado = len(links) > LIMITE_PLAYLIST
                if cortado:
                    links = links[:LIMITE_PLAYLIST]

                for link in links:
                    sala.voice_session.adicionar_musica(link)
                
                aviso_corte = f" (Máximo de {LIMITE_PLAYLIST} músicas atingido!)" if cortado else ""
                await bot.rest.create_message(
                    channel_id,
                    f"🎶 {len(links)} música(s) adicionada(s) à fila!{aviso_corte}"
                )
            else:
                await bot.rest.create_message(
                    channel_id,
                    "⚠️ Informe pelo menos um link ou **pasta do Google Drive**."
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
                lista = "\n".join(f"{i+1}. `{url[:50]}...`" if len(url) > 50 else f"{i+1}. `{url}`" for i, url in enumerate(fila[:10]))
                await bot.rest.create_message(channel_id, f"📋 **Fila Atual (Top 10):**\n{lista}")


if __name__ == "__main__":
    bot.run()
