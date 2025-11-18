# 📑 Descritivo da Implementação do Protocolo Serial  
*(Stop-and-Wait com Checkpointing)*

Este documento detalha a arquitetura e a execução do protocolo de transferência de arquivos serial implementado em **Python**, baseado no modelo **Stop-and-Wait com Reenvio Automático (PAR)** e complementado com **Checkpointing (Retomada Automática)**.

---

## 1. ⚡ Camada Física

A camada física foi construída manualmente utilizando **conectores DB9** e **comunicação serial direta (sem modem)** entre dois computadores.

### 🔧 Cabo Serial (DB9 Direto)

O cabo confeccionado utiliza **apenas três fios (2, 3 e 5)**, suficientes para transmissão e recepção de dados e referência de terra.

| **Pino DB9** | **Sinal** | **Função** | **Conexão** |
|--------------|-----------|-------------|--------------|
| 2 | RXD | Recebe Dados | Conectado ao pino 3 do outro lado (TXD) |
| 3 | TXD | Transmite Dados | Conectado ao pino 2 do outro lado (RXD) |
| 5 | GND | Terra (Sinal Comum) | Conectado diretamente ao pino 5 do outro conector |

📎 **Observação:**  
O controle de fluxo via hardware (**RTS/CTS**) não foi utilizado (`rtscts=False`).  
Todo o controle de fluxo e retransmissão é feito **via software**, na camada de enlace.

### ⚙️ Configuração Física no Código

| **Parâmetro** | **Configuração** | **Descrição** |
|---------------|------------------|----------------|
| Meio Físico | Cabo Serial DB9 (fios 2, 3 e 5) | Comunicação ponto a ponto entre duas máquinas |
| Porta | `-p /dev/ttyUSB0` ou `COM3` | Porta serial física ou virtual |
| Baud Rate | `-b 115200` | Taxa de transmissão |
| Controle de Fluxo | `rtscts=False` | Controle de fluxo via protocolo (Stop-and-Wait) |
| Formato de Quadro Físico | `8N1` | 8 bits de dados, sem paridade, 1 stop bit |

---

## 2. 🔗 Camada de Enlace

Responsável por garantir a entrega confiável e ordenada dos dados, utilizando o mecanismo **Stop-and-Wait ARQ** (Positive Acknowledgement with Retransmission).

### 📦 Estrutura do Quadro

| **Campo** | **Tamanho (Bytes)** | **Descrição** |
|-----------|----------------------|----------------|
| Nº Sequência | 1 | Alterna entre 0 e 1. Detecta duplicatas |
| CRC32 | 4 | Verificação de integridade (IEEE 802.3) |
| Tamanho Real | 4 | Tamanho do payload |
| Dados | Variável (até 100) | Bloco de bytes do arquivo |
| **Total** | **109 bytes** | 1 + 4 + 4 + 100 |

### 🔍 Controle de Fluxo e Erros

| **Sinal** | **Valor** | **Função** |
|-----------|-----------|-------------|
| ACK | `b'A'` | Quadro recebido corretamente |
| NAK | `b'N'` | Quadro incorreto (CRC ou sequência errada) |
| Timeout | — | Após 3 segundos sem resposta, retransmite |

#### 🔄 Lógica Stop-and-Wait

1. Emissor envia um bloco e aguarda confirmação.  
2. Receptor verifica CRC e sequência:  
   - Correto → envia **ACK**  
   - Erro → envia **NAK**  
   - Duplicado → reenvia **ACK** e ignora  
3. Emissor alterna número de sequência (`current_seq_num = 1 - current_seq_num`).

---

## 3. 🖥️ Camada de Aplicação

Gerencia a **inicialização**, **checkpointing** e **finalização** da transferência.

### 🤝 Handshake e Retomada

| **Sinal** | **Descrição** |
|-----------|----------------|
| `START:<filename>` | Solicita início da transmissão |
| `ACK_STATUS:<block_id>` | Informa o último bloco salvo (checkpoint) |
| `END\n` | Indica término da transmissão |

### 📌 Checkpointing

- **Emissor:** lê o último bloco salvo e continua dali.  
- **Receptor:** salva progresso em `<arquivo>.temp`.  
- **Interrupção (Ctrl+C):** mantém `.temp` para retomada posterior.  
- **Conclusão:** remove `.temp` após sinal `END\n`.

---

## 4. 🧩 Estrutura de Código

| **Componente** | **Função** |
|----------------|------------|
| `calculate_crc32()` | Gera CRC32 (IEEE 802.3) |
| `emissor_handler()` | Gerencia envio, ACKs e retransmissões |
| `receptor_handler()` | Lida com recepção, CRC e checkpoint |
| `save_checkpoint()` / `load_checkpoint()` | Armazenam progresso da recepção |
| `signal_handler()` | Detecta Ctrl+C e garante encerramento limpo |

---

## 5. 🚀 Execução do Protocolo

Necessário ter `protocolo.py` e o arquivo `biro.png` em ambos os computadores (ou pelo menos no PC Emissor).

### ▶️ Guia de Execução (Modo Real)

📌 **Requisito:** O **Receptor** deve ser iniciado antes do **Emissor** para que o handshake inicial ocorra corretamente.

#### 🖥️ PC A — Emissor

| **Ambiente** | **Porta Exemplo** | **Comando** |
|--------------|-------------------|--------------|
| Windows (PowerShell/CMD) | COM3 | `python protocolo.py emissor -p COM3 -b 115200 -f biro.png` |
| Linux/WSL | /dev/ttyUSB0 | `python3 protocolo.py emissor -p /dev/ttyUSB0 -b 115200 -f biro.png` |

#### 🖥️ PC B — Receptor

| **Ambiente** | **Porta Exemplo** | **Comando** |
|--------------|-------------------|--------------|
| Windows (PowerShell/CMD) | COM4 | `python protocolo.py receptor -p COM4 -b 115200` |
| Linux/WSL | /dev/ttyUSB1 | `python3 protocolo.py receptor -p /dev/ttyUSB1 -b 115200` |

---

📦 **Instalação de dependências:**

```bash
sudo apt install python3-tk python3-serial
```

---

## 6. ✅ Resumo Geral de Conformidade

O protocolo atende aos requisitos definidos para transferência de arquivos serial, garantindo **confiabilidade** e **robustez**.

| **Categoria** | **Detalhamento** |
|---------------|------------------|
| 📡 Camadas do Protocolo | Física: RS-232 (ou socat) com baud rate ajustável.<br>Enlace: Stop-and-Wait com CRC32 e retransmissão automática.<br>Aplicação: Checkpointing persistente via `.temp` e retomada de transmissão. |
| 🧩 Requisitos Atendidos | Implementação funcional e demonstrável das três camadas, com código Python executável em Linux/WSL e Windows. |
