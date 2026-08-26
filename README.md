# 🎵 Bot de Música — Nerimity

Bot de música leve para servidores do Nerimity, com suporte a links diretos, pastas do Google Drive, pastas do Dropbox e itens do Internet Archive.

## Requisitos

- Python 3.10+
- Dependências:
  ```
  pip install aiortc av numpy nerimity-sdk
  ```
- FFmpeg instalado no sistema (usado internamente pelo PyAV para decodificar os áudios).

## Configuração inicial

Antes de rodar, edite as constantes no topo do arquivo `Kmusic.py`:

| Constante | O que é |
|---|---|
| `TOKEN` | Token do bot no Nerimity |
| `SENHA_MESTRE` | Senha usada no comando `!master` para resetar o bot remotamente |

Depois, rode o bot:

```
python Kmusic.py
```

## Conectando o bot a um servidor

O bot é configurado por **mensagem direta (DM)**, não por comando no servidor:

1. Envie uma DM para o bot com `!config` (ou qualquer mensagem, se ainda não estiver configurado).
2. Ele vai pedir os IDs do canal de texto e do canal de voz, no formato:
   ```
   ID TEXTO: 123456789
   ID VOZ: 987654321
   ```
3. Envie essa mensagem preenchida com os IDs do seu servidor.
4. O bot entra no canal de voz e confirma a conexão. A partir daí, os comandos abaixo funcionam no canal de texto configurado.

Você pode repetir o processo a qualquer momento para reconfigurar ou trocar de canal.

## Comandos (no canal do servidor)

| Comando | Função |
|---|---|
| `!play [link/pasta]` | Adiciona música(s) à fila e começa a tocar |
| `!play embaralhar [link/pasta]` | Adiciona embaralhado, escolhendo até 50 músicas aleatórias da lista encontrada |
| `!tocar` / `!playlist` | Sinônimos de `!play` |
| `!skip` / `!pular` | Pula a música atual |
| `!stop` / `!parar` | Para a reprodução e limpa toda a fila |
| `!fila` / `!queue` | Mostra quantas músicas restam na fila |
| `!sair` | Desconecta o bot do canal de voz |

## Comandos (em DM)

| Comando | Função |
|---|---|
| `!config` | Inicia/reinicia o processo de conexão a um servidor |
| `!sair [ID do canal de voz]` | Desconecta o bot de um canal de voz específico |
| `!master [senha]` | Desconecta o bot de **todos** os canais e servidores de uma vez (reset total) |

## Fontes de música suportadas

- **Links diretos** de áudio (`.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`, `.opus`, `.wma`, `.aac`, `.webm`)
- **Pastas do Google Drive** (`.../folders/...`) — o bot lista e resolve os links de download de cada arquivo automaticamente
- **Arquivo único do Google Drive** (`.../file/d/...`)
- **Pastas compartilhadas do Dropbox** (`/sh/...` ou `/scl/fo/...`)
- **Link direto do Dropbox** (ajusta automaticamente para `dl=1`)
- **Itens do Internet Archive** (`archive.org/details/...` ou `/download/...`)

No `!play`, pode misturar vários links/pastas na mesma mensagem, separados por espaço.

- Limite de **50 músicas por vez** adicionadas à fila (playlists maiores são cortadas, ou amostradas aleatoriamente se usado `embaralhar`).

## Como funciona a reprodução (importante pra estabilidade)

Para evitar oscilação de velocidade (comum com links de rede instáveis, como Google Drive) e problemas de performance em máquinas mais fracas:

- Cada música é **baixada e decodificada por completo antes de começar a tocar** (pré-buffer de 100%). Isso significa que pode levar alguns segundos a mais para a música começar, mas ela toca sem travar ou acelerar depois.
- O bot avisa **"⏳ Carregando música..."** enquanto isso acontece, e **"▶️ Tocando próxima música..."** quando libera pra tocar.
- Se um link estiver realmente quebrado/muito lento (mais de 3 minutos sem completar o carregamento), o bot desiste e segue com o que conseguiu carregar.
- Se a música travar durante a reprodução por mais de 20 segundos seguidos, ela é pulada automaticamente e o bot avisa no canal.
- Suporta reconexão automática em caso de queda momentânea de conexão durante o download.

## Limitações conhecidas

- Um bot só toca em **um canal de voz por servidor** de cada vez (uma "sala" por canal de texto/voz configurado).
- Usuários podem ser bloqueados de configurar o bot via `USUARIOS_BLOQUEADOS` (lista de IDs no topo do código).
- O carregamento 100% antes de tocar consome mais memória por música (até ~13 minutos de áudio guardado, ~76MB no pior caso) — adequado para máquinas com pouca RAM, mas vale monitorar se tocar músicas muito longas.
