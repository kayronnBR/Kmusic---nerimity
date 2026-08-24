# 🎵 Kmusic - Bot de Música para Nerimity

O **Kmusic** é um bot de música e rádio para a plataforma Nerimity. Ele utiliza a biblioteca `aiortc` para comunicação WebRTC de baixa latência e o `yt-dlp` para extrair e reproduzir áudio de centenas de plataformas e transmissões de rádio ao vivo.

---

## ⚡ Recursos

* **Configuração simplificada por DM:** Vincule canais de texto e voz enviando uma mensagem no privado do bot.
* **Amplo suporte a mídias:** Suporta YouTube, SoundCloud, Twitch, rádios ao vivo e links diretos de áudio.
* **Transmissão WebRTC Contínua:** Mantém a conexão de áudio estável mesmo durante a troca de músicas.
* **Controle total de fila:** Comandos para adicionar, pular, limpar a fila e visualizar as próximas faixas.

---

## 🌐 Plataformas e Aplicativos Suportados

Como o bot utiliza o **yt-dlp** e decodificação via FFmpeg, ele é compatível com milhares de sites de mídia e formatos brutos:

| Plataforma / Tipo | Conteúdos Suportados |
| :--- | :--- |
| **YouTube** | Vídeos, Lives / Rádios 24/7 e Shorts |
| **SoundCloud** | Faixas individuais, Álbuns e Sets |
| **Twitch** | Transmissões ao vivo (Livestreams) |
| **Bandcamp & Mixcloud** | Músicas, Sets de DJ e Podcasts |
| **Vimeo & Dailymotion** | Vídeos e conteúdos em áudio |
| **Rádios Web (Icecast / Shoutcast)** | Fluxos contínuos (`.m3u`, `.pls`, portas `:8000`, etc.) |
| **Arquivos Diretos de Áudio** | Links diretos terminados em `.mp3`, `.aac`, `.ogg`, `.flac`, `.wav` |

---

## 📋 Pré-requisitos

Antes de iniciar, certifique-se de ter instalado em seu sistema:

1. **Python 3.10+**
2. **FFmpeg** (Obrigatório para o processamento de áudio via `av` e `yt-dlp`).

### Instalação do FFmpeg (Linux / Debian / Ubuntu):
```bash
sudo apt update
sudo apt install ffmpeg -y

```

---

## 📦 Instalação



1. **Crie e ative um ambiente virtual (Venv):**
```bash
python3 -m venv ~/venv

```


2. **Instale as dependências:**
```bash
~/venv/bin/pip install aiortc av numpy nerimity_sdk yt-dlp

```



---

## ⚙️ Configuração

Abra o arquivo `Kmusic.py` e insira suas credenciais nas variáveis iniciais:

```python
TOKEN = "SEU_TOKEN_DO_BOT_AQUI"
SENHA_MESTRE = "SUA_SENHA_AQUI"

```

---

## 🚀 Como Executar

Execute o script utilizando o Python do seu ambiente virtual:

```bash
~/venv/bin/python3 Kmusic.py

```

---

## 📖 Como Usar

### 1. Configuração Inicial (Na DM do Bot)

Ao mandar qualquer mensagem privada para o bot, ele exibirá um tutorial de ajuda.

1. Envie `!config` na conversa privada (DM) do bot.
2. Envie o **ID do Canal de Texto** onde os comandos de música serão digitados.
3. Envie o **ID do Canal de Voz** onde o bot deve entrar.

---

### 2. Comandos do Bot

| Comando | Onde Usar | Descrição |
| --- | --- | --- |
| `!config` | DM | Inicia o assistente de configuração de sala. |
| `!sair` | Servidor | Desconecta o bot de um canal de voz. |
| `!master <SENHA>` | DM | Força o bot a sair de todas as chamadas ativas. |
| `!play <LINK/NOME>` / `!tocar <LINK>` | Servidor | Adiciona uma música ou rádio à fila. |
para rodar play lista mande assim:
!play
https://youtu.be/csfakKPxtVs?si=eenldU4UpIzhCQMU
https://youtu.be/zXQZGA6MhJA?si=p9Mwf7YuDAxe542R
https://youtu.be/MPBtNkkgwCk?si=CBTlqDZrZyNIY3z4
| `!skip` / `!pular` | Servidor | Pula a música/rádio que está tocando. |
| `!fila` / `!queue` | Servidor | Exibe a lista das próximas 10 faixas. |
| `!stop` / `!parar` | Servidor | Para a reprodução imediatamente e limpa a fila. |

---

## 🛠️ Solução de Problemas

* **O bot entra na call mas não emite som:**
Certifique-se de que o **FFmpeg** está instalado no sistema operacional host. O ícone de microfone mutado na plataforma Nerimity é um comportamento nativo da interface e não impede o envio de áudio.
* **Erro `ModuleNotFoundError: No module named 'yt_dlp'`:**
Execute a instalação dos pacotes diretamente pelo caminho do seu ambiente virtual: `~/venv/bin/pip install yt-dlp`.

```

```
