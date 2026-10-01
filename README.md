# Sinais cripto — versão online

Roda sozinho no GitHub, de graça, sem IA, sem conta em corretora e sem precisar do seu PC ligado.

- **GitHub Actions** roda a análise às 21h11 (logo após o fechamento diário) e a cada 4 horas.
- **Telegram** recebe os pontos de compra e venda e um resumo por dia.
- **GitHub Pages** mostra o painel, com um botão para atualizar os preços na hora.

## Regras (v2, escolhidas no backtest de carteira de 29/09/2026)

| | Compra | Venda |
|---|---|---|
| **Núcleo BTC** (segurar) | diário do BTC fecha **acima** da EMA200 | diário do BTC fecha **abaixo** da EMA200 |
| **Rompimento** (8 moedas) | diário fecha acima da **máxima de 20 dias** (moeda e BTC acima da EMA200) | diário fecha abaixo da **mínima de 10 dias**, ou stop inicial (2 × ATR) |

Uma carteira simulada de 1.000 USDT (50% núcleo / 50% rompimentos) acompanha os sinais para medir se funcionam.

## Instalação (uma vez, ~15 minutos)

### 1. Criar a conta e o repositório
1. Crie uma conta grátis em **github.com**.
2. Clique em **New repository**. Nome: `sinais-cripto`. Marque **Public** (o plano grátis só publica o painel de repositórios públicos; nenhuma senha fica no código).
3. Marque **Add a README file** e clique em **Create repository**.

### 2. Enviar os arquivos
1. No repositório, clique em **Add file → Upload files**.
2. Abra a pasta `online` no Windows, selecione **tudo que está dentro dela** e arraste para a página.
3. Clique em **Commit changes**.

### 2b. Criar o agendamento (arquivo do GitHub Actions)
1. No repositório, clique em **Add file → Create new file**.
2. No nome, digite exatamente: `.github/workflows/sinais.yml` (as barras criam as pastas).
3. Abra `workflow_sinais.yml` (na pasta `online`) no Bloco de Notas, copie tudo e cole no GitHub.
4. Clique em **Commit changes**.

### 3. Guardar as chaves do Telegram (em segredo)
**Settings → Secrets and variables → Actions → New repository secret**, duas vezes:
- Nome `TELEGRAM_TOKEN`, valor: o token do seu bot.
- Nome `TELEGRAM_CHAT_ID`, valor: seu chat ID.

### 4. Ligar o painel
**Settings → Pages** → em *Branch* escolha `main` e a pasta `/docs` → **Save**.
Em 1–2 minutos o painel fica em `https://SEU-USUARIO.github.io/sinais-cripto/`.

### 5. Primeira execução
**Actions → Sinais → Run workflow**. Em ~1 minuto aparece o ✅ verde e o painel mostra os dados.

Depois disso, **feche o `rodar_bot.bat` no PC**: a versão online assume.

## Uso

- **Atualizar preços** (no painel): busca a cotação atual direto no seu navegador.
- **Rodar análise** (no painel): abre o GitHub; toque em *Run workflow* para uma análise completa agora.
- **Mudar parâmetros**: edite `config.yaml` pelo próprio site do GitHub (ícone de lápis).
- **Liberar a trava de −15%**: edite `docs/data/estado.json` e troque o valor de `"locked"` por `null`.

## Observações

- A Bybit bloqueia servidores em nuvem; por isso os preços vêm da Binance (ou OKX, se precisar). Os preços diários entre as grandes corretoras são praticamente iguais.
- O GitHub pode atrasar execuções agendadas em alguns minutos nos horários de pico.
- Carteira simulada, sem dinheiro real. Não é recomendação de investimento.

## Aviso de aporte (longo prazo)
`sinais/aporte.py` compara o preço do BTC com a média de 200 dias (múltiplo de Mayer) e avisa no Telegram:
- 🟢 **PREÇO VANTAJOSO** quando o preço fecha abaixo da média (sugere 1,5× o aporte do mês) ou abaixo de 0,8× a média (2×);
- 🟠 **PREÇO CARO** acima de 2,4× a média (sugere 0,5×);
- 📅 lembrete no dia `aporte.reminder_day` de cada mês com a faixa atual.
Depois de um aviso, fica 30 dias em silêncio, a não ser que o preço fique ainda mais barato. É só aviso: a compra é feita por você, na sua corretora, e não entra na carteira simulada. Ajustes em `config.yaml` → `aporte`.
