# Conectar o Gemini Wrapper ao Pluely

O Pluely (v1.1.0) usa provedores customizados (Dev space). O wrapper expõe um endpoint
compatível com OpenAI em `http://localhost:8000` (porta única, compartilhada com o Auto-Job).

## Provedor de IA (já cadastrado)

- Provedor: `gemini-wrapper`, **sem streaming** (o streaming do wrapper duplica/embaralha texto).
- Response path: `choices[0].message.content`
- cURL: `curl http://localhost:8000/v1beta/openai/chat/completions` (corpo OpenAI padrão)
- **Campo MODEL do Pluely = `gemini-flash`** — é isso que escolhe o modelo por requisição.

O Pluely monta o próprio corpo da requisição: só o campo `model` e a chave chegam ao proxy
(headers e campos extras do curl são ignorados).

### Escolha de modelo por requisição (proxy)

| `model` enviado          | Efeito                                                    |
|--------------------------|-----------------------------------------------------------|
| `gemini-flash` / `flash` | Flash (rápido, ~8–11 s)                                   |
| `gemini-pro` / `pro`     | Pro (lento; pode travar nesta conta)                      |
| qualquer outro nome      | usa a config compartilhada `.selected_model` (Auto-Job)   |

Também aceito: header `x-gemini-model` e campo `gemini_model` no corpo.
O proxy só reaplica o seletor do Gemini quando o modo muda (~4 s), então Pluely (Flash) e
Auto-Job (config) podem alternar sem interferir um no outro.

### Velocidade (medido com o proxy livre, Flash)

- Típico: **5–10 s** por resposta (média 7,0 s em 10 perguntas); ~3–5 s disso é o próprio Gemini até o primeiro texto.
- Proxy responde também em `::1` (mesma porta 8000): clientes que resolvem `localhost` para IPv6 primeiro
  perdiam ~2 s por conexão.
- Reset de chat após cada resposta espera a página ficar pronta em vez de 1 s fixo.
- Proteções contra travadas do Gemini (modos Flash/Lite): sem texto em 25 s → refaz uma vez em chat novo
  (limite do retry 60 s); texto parado por 6 s com a página ainda "gerando" → considera completo.
  O Pro continua com espera longa (até 240 s), pois é naturalmente mais lento.
- `gemini-lite` existe na conta e é um pouco mais rápido, mas travou com frequência: **use `gemini-flash`**.
- Ajustes por variável de ambiente: `GEMINI_MAX_WAIT_SEC` (240), `GEMINI_FAST_STALL_SEC` (25),
  `GEMINI_FAST_RETRY_MAX_SEC` (60), `GEMINI_FAST_IDLE_SEC` (6), `GEMINI_SLOW_IDLE_SEC` (25).
- `logs/pool.log` registra `[Timing] ready/typed/sent/first_text/done` por requisição.

## Speech-to-Text (cadastrado, não testado)

`POST http://localhost:8000/v1/speech:recognize` (LINEAR16 16 kHz em base64; WAV completo também é aceito).
Response path: `results[0].alternatives[0].transcript`

## Login do Gemini

Se `logs/pool.log` mostrar logout/captcha, refaça o login:

```
venv\Scripts\python.exe sync_cookies.py user2 --login
```

## Arquivos

- `Gemini Config.bat` – console de configuração (igual ao do Auto-Job); depois sobe o proxy oculto e fecha.
- `Gemini Wrapper.bat` / `Gemini Wrapper (hidden).vbs` – iniciam o proxy direto (com/sem console).
- `Stop Gemini Wrapper.bat/.ps1` – parada graciosa (fecha o Chrome limpo; força após 20 s).
- `logs/pool.log` – atividade do pool Playwright; `logs/empty_response.png` – captura quando vem resposta vazia.
- `GEMINI_MAX_WAIT_SEC` (env, padrão 240) – tempo máximo esperando uma resposta.
- `%LOCALAPPDATA%\PluelyGemini\` – launcher do Pluely (`Pluely + Gemini.vbs/.ps1`) e `configure_pluely.py`.
  Rodar `configure_pluely.py` de novo redefine os provedores; depois confira o campo MODEL (`gemini-flash`).
