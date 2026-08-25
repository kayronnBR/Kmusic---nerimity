# 🎵 Kmusic - Bot de Música para Nerimity

O **Kmusic** é um bot de música e rádio para a plataforma Nerimity. Ele utiliza a biblioteca `aiortc` para comunicação WebRTC de baixa latência.

---

## ⚡ Recursos

* **Configuração simplificada por DM:** Vincule canais de texto e voz enviando uma mensagem no privado do bot.
* **Amplo suporte a mídias:** escute musicas do Google drive, Dropbox, rádio online, .mp3, .ogg e outros formatos de áudio
* **Transmissão WebRTC Contínua:** Mantém a conexão de áudio estável mesmo durante a troca de músicas.
* **Controle total de fila:** Comandos para adicionar, pular, limpar a fila e visualizar as próximas faixas.

---

## 🌐 Plataformas e Aplicativos Suportados

Como o bot utiliza o **yt-dlp** e decodificação via FFmpeg, ele é compatível com milhares de sites de mídia e formatos brutos:

| Plataforma / Tipo | Conteúdos Suportados |
| :--- | :--- |
| **Google drive** | pasta de música ou link separado|
| **Dropbox** | consegue reproduzir 1 arquivo por ver sem possibilidade de playlist automático|
| **Rádios Web (Icecast / Shoutcast)** | Fluxos contínuos (`.m3u`, `.pls`, portas `:8000`, etc.) |
| **Arquivos Diretos de Áudio** | Links diretos terminados em `.mp3`, `.aac`, `.ogg`, `.flac`, `.wav` |

---

## 📋 Pré-requisitos

Antes de iniciar, certifique-se de ter instalado em seu sistema:

1. **Python 3.10+**


```

---

## 📦 Instalação



1. **Crie e ative um ambiente virtual (Venv):**
```bash
python3 -m venv ~/venv

```


2. **Instale as dependências:**
```bash
~/venv/bin/pip install aiortc av numpy nerimity_sdk

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
---

### 2. Comandos do Bot

| Comando | Onde Usar | Descrição |
| --- | --- | --- |
| `!config` | DM | Inicia o assistente de configuração de sala. |
| `!sair` | Servidor | Desconecta o bot de um canal de voz. |
| `!master <SENHA>` | DM | Força o bot a sair de todas as chamadas ativas. |
| `!play <LINK/NOME>` / `!tocar <LINK>` | Servidor | Adiciona uma música ou rádio à fila. |
para rodar play lista mande assim:
!play LINK-DA-PASTA-DO-GOOGLE-DRIVE
ele vai puxar das as músicas, se passa de 50 ele não pega o resto por conta do limite
| `!skip` / `!pular` | Servidor | Pula a música/rádio que está tocando. |
| `!fila` / `!queue` | Servidor | Exibe a lista das próximas 10 faixas. |
| `!stop` / `!parar` | Servidor | Para a reprodução imediatamente e limpa a fila. |
| `!extrair` / | DM | coloque o link da pasta de músicas suas no comando e ele vai manda a lista completa |

---

## 🛠️ Solução de Problemas

* **O bot entra na call mas não emite som:**
Certifique-se de que o **FFmpeg** está instalado no sistema operacional host. O ícone de microfone mutado na plataforma Nerimity é um comportamento nativo da interface e não impede o envio de áudio.
* **Erro `ModuleNotFoundError: No module named 'yt_dlp'`:**
Execute a instalação dos pacotes diretamente pelo caminho do seu ambiente virtual: `~/venv/bin/pip install yt-dlp`.

```

```
