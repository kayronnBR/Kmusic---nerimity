"""
Bot de Música Ultra Leve para Nerimity.
Mensagens simplificadas de reprodução e aviso de conexão no !play.
"""

import asyncio
import fractions
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, Optional, List, TypedDict

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
TOKEN = "SEU_TOKEN_DO_BOT_AQUI"

EXTENSOES_AUDIO = ('.mp3', '.wav', '.m4a', '.flac', '.ogg', '.opus', '.wma', '.aac', '.webm')

COMANDO_CONFIG = "!config"
COMANDO_SAIR = "!sair"
COMANDO_MESTRE = "!master"
SENHA_MESTRE = "SUA_SENHA_AQUI"

USUARIOS_BLOQUEADOS = set()

PEER_CONNECT_TIMEOUT = 15
LIMITE_PLAYLIST = 50

# Áudio PCM mono a 48kHz em blocos de 20ms ocupa pouquíssima memória
# (~1.9KB por bloco, ~23MB pra uma música de 4 minutos), então mesmo
# num aparelho com 2GB de RAM dá pra guardar a música inteira antes de tocar.
#
# Com a placa de rede ruim, carregar 100% antes de começar evita qualquer
# chance de faltar dado NO MEIO da reprodução (o preço é só esperar mais
# no início). Por isso o alvo de pré-buffer é igual ao máximo: só libera
# pra tocar quando a música terminar de baixar por completo.
BUFFER_FRAMES_MAXIMO = 40000      # ~13 minutos de áudio decodificado guardado por música
FRAMES_PREBUFFER_ALVO = BUFFER_FRAMES_MAXIMO  # espera carregar 100% antes de tocar
TIMEOUT_PREBUFFER = 180.0         # tempo máx. de espera pelo carregamento completo
TIMEOUT_CURTO_RECV = 2.0          # cada tentativa de leitura espera no máx. 2s
TIMEOUT_TOTAL_TRAVADO = 20.0      # tempo total sem áudio até desistir e pular

# Servidores STUN públicos e estáveis (removidos os servidores TURN com credenciais expiradas)
ICE_SERVERS = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="stun:stun1.l.google.com:19302"),
    RTCIceServer(urls="stun:stun2.l.google.com:19302"),
]

bot = Bot(token=TOKEN)


class ItemMusica(TypedDict, total=False):
    url: str
    headers: str


def _obter_html_gdrive(folder_id: str) -> str:
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        return response.read().decode('utf-8', errors='ignore')


def _resolver_download_gdrive(file_id: str) -> ItemMusica:
    url_base = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers_req = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        cookiejar = urllib.request.HTTPCookieProcessor()
        opener = urllib.request.build_opener(cookiejar)
        req = urllib.request.Request(url_base, headers=headers_req)
        with opener.open(req, timeout=12) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' not in content_type:
                return {"url": url_base}
            html = resp.read().decode('utf-8', errors='ignore')

        cookie_header = "; ".join(f"{c.name}={c.value}" for c in cookiejar.cookiejar)

        match_form = re.search(r'action="(https://drive\.usercontent\.google\.com/download[^"]+)"', html)
        if match_form:
            form_url = match_form.group(1).replace('&amp;', '&')
            campos = dict(re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html))
            if campos:
                sep = '&' if '?' in form_url else '?'
                form_url = f"{form_url}{sep}{urllib.parse.urlencode(campos)}"
            item: ItemMusica = {"url": form_url}
            if cookie_header:
                item["headers"] = f"Cookie: {cookie_header}\r\n"
            return item

        match_confirm = re.search(r'confirm=([0-9A-Za-z_-]+)', html)
        if match_confirm:
            item = {"url": f"{url_base}&confirm={match_confirm.group(1)}"}
            if cookie_header:
                item["headers"] = f"Cookie: {cookie_header}\r\n"
            return item
    except Exception as e:
        print(f"Aviso: não consegui contornar a página de aviso do Drive para {file_id}: {e}")

    return {"url": url_base}


async def extrair_todos_links_gdrive(candidato: str) -> List[ItemMusica]:
    match_pasta = re.search(r'folders/([a-zA-Z0-9_-]+)', candidato)
    if not match_pasta:
        return []
    folder_id = match_pasta.group(1)

    try:
        html = await asyncio.to_thread(_obter_html_gdrive, folder_id)
        ids: List[str] = []
        vistos = set()

        padrao_json = r'\["([a-zA-Z0-9_-]{25,})"'
        for fid in re.findall(padrao_json, html):
            if fid not in vistos:
                vistos.add(fid)
                ids.append(fid)

        if not ids:
            padrao_generico = r'/file/d/([a-zA-Z0-9_-]+)'
            ids = list(dict.fromkeys(re.findall(padrao_generico, html)))

        resultado = await asyncio.gather(
            *(asyncio.to_thread(_resolver_download_gdrive, fid) for fid in ids)
        )
        return list(resultado)
    except Exception as e:
        print(f"Erro ao ler pasta do Drive: {e}")
        return []


def _obter_html_dropbox_pasta(url_pasta: str) -> str:
    req = urllib.request.Request(
        url_pasta,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        return response.read().decode('utf-8', errors='ignore')


async def extrair_todos_links_dropbox_pasta(url_pasta: str) -> List[ItemMusica]:
    try:
        html = await asyncio.to_thread(_obter_html_dropbox_pasta, url_pasta)
        resultado: List[ItemMusica] = []
        vistos = set()
        ext_regex = "|".join(e.lstrip(".") for e in EXTENSOES_AUDIO)

        padrao_scl = rf'(/scl/fi/[a-zA-Z0-9]+/[^"\'?\s]+\.(?:{ext_regex}))\?[^"\'\s]*?rlkey=([a-zA-Z0-9]+)'
        for caminho, rlkey in re.findall(padrao_scl, html, re.IGNORECASE):
            if caminho in vistos:
                continue
            vistos.add(caminho)
            resultado.append({"url": f"https://www.dropbox.com{caminho}?rlkey={rlkey}&dl=1"})

        padrao_s = rf'(/s/[a-zA-Z0-9]+/[^"\'?\s]+\.(?:{ext_regex}))'
        for caminho in re.findall(padrao_s, html, re.IGNORECASE):
            if caminho in vistos:
                continue
            vistos.add(caminho)
            resultado.append({"url": f"https://www.dropbox.com{caminho}?dl=1"})

        return resultado
    except Exception as e:
        print(f"Erro ao ler pasta do Dropbox: {e}")
        return []


def _obter_metadata_archive(identifier: str) -> dict:
    url = f"https://archive.org/metadata/{identifier}"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode('utf-8', errors='ignore'))


async def extrair_todos_links_archive(candidato: str) -> List[ItemMusica]:
    match = re.search(r'archive\.org/(?:details|download)/([a-zA-Z0-9_.\-]+)', candidato)
    if not match:
        return []
    identifier = match.group(1)

    try:
        data = await asyncio.to_thread(_obter_metadata_archive, identifier)
        resultado: List[ItemMusica] = []
        for f in data.get("files", []):
            nome = f.get("name") or ""
            formato = (f.get("format") or "").lower()
            eh_audio = nome.lower().endswith(EXTENSOES_AUDIO) or "audio" in formato
            if eh_audio:
                url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(nome)}"
                resultado.append({"url": url})
        return resultado
    except Exception as e:
        print(f"Erro ao ler item do Internet Archive: {e}")
        return []


async def extrair_links_da_lista(texto: str) -> List[ItemMusica]:
    candidatos = re.split(r'[\s,]+', texto.strip())
    links_prontos: List[ItemMusica] = []

    for c in candidatos:
        if c.startswith(('http://', 'https://')):
            if "drive.google.com" in c and "folders/" in c:
                itens = await extrair_todos_links_gdrive(c)
                links_prontos.extend(itens)
            elif "drive.google.com/file/d/" in c:
                match = re.search(r'/d/([a-zA-Z0-9_-]+)', c)
                if match:
                    fid = match.group(1)
                    item = await asyncio.to_thread(_resolver_download_gdrive, fid)
                    links_prontos.append(item)
            elif "dropbox.com" in c and ("/sh/" in c or "/scl/fo/" in c):
                itens = await extrair_todos_links_dropbox_pasta(c)
                links_prontos.extend(itens)
            elif "dropbox.com" in c:
                url_final = c.replace("?dl=0", "?dl=1")
                if "&dl=1" not in url_final and "?dl=1" not in url_final:
                    url_final += "&dl=1" if "?" in url_final else "?dl=1"
                links_prontos.append({"url": url_final})
            elif "archive.org" in c and any(c.split('?')[0].lower().endswith(ext) for ext in EXTENSOES_AUDIO):
                links_prontos.append({"url": c})
            elif "archive.org" in c and ("/details/" in c or "/download/" in c):
                itens = await extrair_todos_links_archive(c)
                links_prontos.extend(itens)
            else:
                links_prontos.append({"url": c})

    return links_prontos


class AudioFileSource:
    def __init__(
        self,
        url: str,
        loop: asyncio.AbstractEventLoop,
        buffer_frames: int = BUFFER_FRAMES_MAXIMO,
        alvo_prebuffer: int = FRAMES_PREBUFFER_ALVO,
        headers: Optional[str] = None,
    ):
        self.url = url
        self.loop = loop
        self.headers = headers
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=buffer_frames)
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        self._stopped = False
        self._finished = False
        self._alvo_prebuffer = min(alvo_prebuffer, buffer_frames)
        # Sinalizado assim que o buffer atinge o alvo, o arquivo termina de baixar,
        # ou ocorre um erro — o que acontecer primeiro.
        self.pronto_evento = asyncio.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"audio-decode-{id(self)}"
        )
        self._thread.start()

    async def aguardar_pronto(self, timeout: float = TIMEOUT_PREBUFFER) -> None:
        try:
            await asyncio.wait_for(self.pronto_evento.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Não travou de vez: apenas seguimos com o que já tiver no buffer.
            pass

    def _sinalizar_pronto_se_necessario(self) -> None:
        if not self.pronto_evento.is_set() and self.queue.qsize() >= self._alvo_prebuffer:
            self.loop.call_soon_threadsafe(self.pronto_evento.set)

    def stop(self) -> None:
        self._stopped = True
        try:
            while True:
                self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    def _run(self) -> None:
        container = None
        try:
            opcoes = {
                'timeout': '8000000',
                'rw_timeout': '12000000',
                'reconnect': '1',
                'reconnect_streamed': '1',
                'reconnect_on_network_error': '1',
                'reconnect_delay_max': '5',
            }
            if self.headers:
                opcoes['headers'] = self.headers
            container = av.open(
                self.url,
                timeout=(8.0, 12.0),
                options=opcoes,
            )
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
                    # Envia para a fila sem bloquear a thread esperando retornos
                    while not self._stopped and self.queue.qsize() >= self.queue.maxsize:
                        time.sleep(0.01)
                    if self._stopped:
                        break
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, r_frame)
                    self._sinalizar_pronto_se_necessario()

                if self._stopped:
                    break
        except Exception as e:
            print(f"Erro ao decodificar link ({self.url}): {e}")
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            try:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
            except Exception:
                pass
            # Garante que quem estiver esperando o pré-buffer não fique preso
            # para sempre se a música for curta, falhar ou já tiver terminado.
            if not self.pronto_evento.is_set():
                try:
                    self.loop.call_soon_threadsafe(self.pronto_evento.set)
                except Exception:
                    pass

    async def recv(self, timeout: Optional[float] = None):
        if self._finished:
            return None
        if timeout:
            frame = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        else:
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

        self.fila: List[ItemMusica] = []
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

    def adicionar_musica(self, item: ItemMusica):
        self.fila.append(item)

    def finalizar_musica_atual(self):
        if self.current_track:
            try:
                self.current_track.stop()
            except Exception:
                pass
        self.current_track = None

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
                    await self._enviar_texto("🏁 Fila finalizada!")
                await asyncio.sleep(1)
                continue

            self._avisou_fila_vazia = False
            musica_atual = self.fila.pop(0)

            try:
                loop = asyncio.get_running_loop()
                nova_faixa = AudioFileSource(
                    musica_atual["url"], loop, headers=musica_atual.get("headers")
                )
                await self._enviar_texto("⏳ **Carregando música...**")
                # Espera carregar 100% antes de liberar pro pump loop tocar,
                # pra absorver as variações de velocidade do link (ex: Google Drive)
                # sem engasgar/acelerar a música depois.
                await nova_faixa.aguardar_pronto()
                self.current_track = nova_faixa
                self._ja_tocou_alguma_musica = True
                await self._enviar_texto("▶️ **Tocando próxima música...**")
            except Exception as e:
                print(f"Erro ao carregar áudio: {e}")
                self.finalizar_musica_atual()

    async def _audio_pump_loop(self):
        FRAME_SIZE = 960
        fifo = av.AudioFifo()
        pts = 0
        start_time = time.perf_counter()

        # Controla há quanto tempo estamos sem receber áudio de verdade da
        # faixa atual, pra só desistir dela depois de um tempo total travado
        # (em vez de um único timeout gigante que engasgava tudo de uma vez).
        faixa_referencia = None
        travado_desde: Optional[float] = None

        while True:
            if self.current_track is not faixa_referencia:
                faixa_referencia = self.current_track
                travado_desde = None

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
                        frame = await self.current_track.recv(timeout=TIMEOUT_CURTO_RECV)
                        travado_desde = None
                    except asyncio.TimeoutError:
                        if travado_desde is None:
                            travado_desde = time.perf_counter()
                        elif time.perf_counter() - travado_desde >= TIMEOUT_TOTAL_TRAVADO:
                            print(f"⏱️ Música travada por mais de {TIMEOUT_TOTAL_TRAVADO:.0f}s, pulando automaticamente.")
                            self.finalizar_musica_atual()
                            travado_desde = None
                            asyncio.create_task(
                                self._enviar_texto("⚠️ Uma música travou ao carregar e foi pulada automaticamente.")
                            )
                        # Ainda dentro da tolerância: só preenche este trecho
                        # com silêncio e tenta de novo no próximo bloco,
                        # mantendo o ritmo/relógio sem saltos.
                    except Exception as e:
                        print(f"Erro ao ler áudio: {e}")
                        self.finalizar_musica_atual()
                        travado_desde = None

                if frame is not None:
                    try:
                        fifo.write(frame)
                    except Exception:
                        pass
                else:
                    if self.current_track is not None and travado_desde is None:
                        # A faixa terminou de verdade (recv devolveu None sem timeout).
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
        try:
            await asyncio.wait_for(
                self.bot._gateway.emit(
                    "voice:signal_send",
                    {"channelId": self.channel_id, "toUserId": to_user_id, "signal": signal},
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            print(f"⚠️ Timeout ao enviar sinal WebRTC para {to_user_id}.")

    async def ao_usuario_entrar(self, payload: dict) -> None:
        if not self._connected: return
        user_id = payload.get("userId")
        if not user_id or user_id == self.my_user_id or user_id in self.peers: return
        try:
            pc = self._nova_conexao(user_id)
            offer = await asyncio.wait_for(pc.createOffer(), timeout=10.0)
            await asyncio.wait_for(pc.setLocalDescription(offer), timeout=10.0)
            await self._enviar_sinal(user_id, {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp})
        except Exception as e:
            print(f"⚠️ Erro ao negociar conexão de voz com {user_id}: {e}")

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
                await asyncio.wait_for(pc.setRemoteDescription(desc), timeout=10.0)
                if signal["type"] == "offer":
                    answer = await asyncio.wait_for(pc.createAnswer(), timeout=10.0)
                    await asyncio.wait_for(pc.setLocalDescription(answer), timeout=10.0)
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
            "👋 **Bot de Música Simplificado**\n\n"
            "⚙️ **Para conectar o bot ao servidor**, envie o modelo preenchido:\n"
            "```\nID TEXTO: \nID VOZ: \n```"
        )

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
            "🎉 Bot conectado com sucesso!\n\nUse no servidor:\n"
            "• `!play [link/pasta]` - Adiciona músicas\n"
            "• `!play embaralhar [link/pasta]` - Embaralha e escolhe até "
            f"{LIMITE_PLAYLIST} músicas aleatórias\n"
            "• `!skip` - Pula a música\n"
            "• `!stop` - Para a música\n"
            "• `!sair` - Desconecta"
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


def _tarefa_segura(coro, nome: str = "handler") -> None:
    async def _executar():
        try:
            await coro
        except Exception as e:
            print(f"⚠️ Erro no handler '{nome}': {e}")

    asyncio.create_task(_executar())


@bot.on("ready")
async def on_ready(me):
    gerenciador.my_user_id = getattr(me, "id", None)
    print("✅ Bot de música pronto!")

@bot.on("voice:user_joined")
async def on_voice_user_joined(payload):
    sala = gerenciador.sala_por_canal_voz(str(payload.get("channelId", "")))
    if sala:
        _tarefa_segura(sala.voice_session.ao_usuario_entrar(payload), "voice:user_joined")

@bot.on("voice:user_left")
async def on_voice_user_left(payload):
    sala = gerenciador.sala_por_canal_voz(str(payload.get("channelId", "")))
    if sala:
        _tarefa_segura(sala.voice_session.ao_usuario_sair(payload), "voice:user_left")

@bot.on("voice:signal_received")
async def on_voice_signal(payload):
    sala = gerenciador.sala_por_canal_voz(str(payload.get("channelId", "")))
    if sala:
        _tarefa_segura(sala.voice_session.ao_receber_sinal(payload), "voice:signal_received")

@bot.on("message:created")
async def on_message(event):
    _tarefa_segura(_processar_mensagem(event), "message:created")


async def _processar_mensagem(event):
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
                primeira_palavra, _, resto = argumento.partition(" ")
                embaralhar = primeira_palavra.lower() in ("embaralhar", "aleatorio", "aleatório", "shuffle")
                argumento_link = resto.strip() if embaralhar else argumento

                if not argumento_link:
                    await bot.rest.create_message(
                        channel_id,
                        "⚠️ Envie um link ou pasta junto com o comando."
                    )
                    return

                itens = await extrair_links_da_lista(argumento_link)
                if not itens:
                    itens = [{"url": argumento_link.strip()}]

                total_encontrado = len(itens)
                if embaralhar:
                    random.shuffle(itens)

                cortado = len(itens) > LIMITE_PLAYLIST
                if cortado:
                    itens = itens[:LIMITE_PLAYLIST]

                for item in itens:
                    sala.voice_session.adicionar_musica(item)

                aviso_corte = (
                    f" ({total_encontrado} encontradas, {LIMITE_PLAYLIST} escolhidas aleatoriamente)"
                    if cortado and embaralhar
                    else f" (Máximo de {LIMITE_PLAYLIST} por vez)" if cortado
                    else ""
                )
                aviso_embaralhado = " 🔀 embaralhadas!" if embaralhar and not cortado else ""
                await bot.rest.create_message(
                    channel_id,
                    f"🎶 {len(itens)} música(s) adicionada(s) à fila!{aviso_corte}{aviso_embaralhado}\n"
                    f"⚠️ *Caso não esteja escutando a música, saia e entre na ligação.*"
                )
            else:
                await bot.rest.create_message(
                    channel_id,
                    "⚠️ Envie um link ou pasta junto com o comando."
                )

        elif comando in ["!skip", "!pular"]:
            sala.voice_session.pular_musica()
            await bot.rest.create_message(channel_id, "⏭️ Faixa pulada.")

        elif comando in ["!stop", "!parar"]:
            sala.voice_session.parar()
            await bot.rest.create_message(channel_id, "🛑 Reprodução interrompida e fila limpa.")

        elif comando == "!sair" and not argumento:
            await gerenciador.sair_da_sala_por_texto(channel_id)

        elif comando in ["!fila", "!queue"]:
            total = len(sala.voice_session.fila)
            await bot.rest.create_message(channel_id, f"📋 **Músicas restantes na fila:** {total}")


if __name__ == "__main__":
    bot.run()
