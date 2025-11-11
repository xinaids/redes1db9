# Descritivo da Implementação do Protocolo Serial (Stop-and-Wait com Checkpointing)

Este documento detalha a arquitetura do protocolo de transferência de arquivos serial implementado em **Python**, baseado no modelo **Stop-and-Wait com Reenvio Automático (PAR)** e robustecido com um sistema de **Checkpointing (Retomada)**.

---

## 1. Camada Física

A camada mais baixa define os parâmetros de comunicação binária sobre o meio físico serial.

| **Parâmetro** | **Configuração no Código** | **Detalhes** |
|----------------|-----------------------------|---------------|
| **Meio Físico** | — | Adaptador USB-Serial ou RS-232. Conexão ponto a ponto entre dois dispositivos (Emissor e Receptor). |
| **Porta** | Argumento `-p, --port` | Identificação da porta (Ex: `COM3`, `/dev/ttyUSB0`, `/dev/pts/1`). |
| **Baud Rate** | Argumento `-b, --baud` (Padrão: 115200) | Taxa de transmissão configurável. |
| **Controle de Fluxo** | `rtscts=False` | Implementação desligada. O controle de fluxo é garantido pelo próprio protocolo Stop-and-Wait (ACK/NAK). |
| **Configuração** | `serial.Serial` | 8 bits de dados, sem paridade, 1 stop bit. |

---

## 2. Camada de Enlace

A Camada de Enlace é responsável por estruturar os dados do arquivo em pacotes (quadros) e garantir a entrega confiável e ordenada através da lógica **Stop-and-Wait**.

### Estrutura do Quadro (Pacote de Dados)

O protocolo utiliza um pacote binário otimizado para a transferência de dados em blocos de até **100 bytes (BLOCK_SIZE)**.

| **Campo** | **Tamanho (Bytes)** | **Descrição** |
|------------|----------------------|----------------|
| Nº Sequência | 1 | (0 ou 1). Essencial para o PAR Stop-and-Wait. Usado para detectar duplicatas. |
| CRC32 | 4 | Código de Redundância Cíclica (IEEE 802.3). Calculado sobre o tamanho real + dados. |
| Tamanho Real | 4 | Tamanho exato dos bytes de dados úteis (payload). `struct.pack('<I', data_len)` |
| Dados | Variável (máx. 100) | Bloco (chunk) de bytes do arquivo. |
| **Tamanho Máximo** | **109 bytes** | (1 + 4 + 4 + 100). |

---

### Controle de Fluxo e Erros (Stop-and-Wait / PAR)

A comunicação é baseada em confirmações de **1 byte**, controlando a progressão do emissor:

| **Sinalizador** | **Valor (1 byte)** | **Função** |
|------------------|--------------------|-------------|
| **ACK** | `b'A'` | Quadro recebido corretamente. Emissor pode avançar. |
| **NAK** | `b'N'` | Quadro incorreto (CRC ou sequência errada). Emissor retransmite. |
| **Timeout** | — | Expiração de `TIMEOUT_SEC = 3s`. Emissor retransmite o quadro. |

#### Lógica de Sequência

- O **Emissor** alterna o número de sequência:  
  `current_seq_num = 1 - current_seq_num`
- O **Receptor** espera `expected_seq_num`.
- Se o quadro for correto → grava, envia ACK, e incrementa `expected_seq_num`.
- Se incorreto → envia NAK.
- Se duplicado → reenvia ACK e ignora os dados (garantindo idempotência).

---

## 3. Camada de Aplicação

Trata da lógica de alto nível: **gerenciamento de arquivos, checkpointing e controle da sessão**.

### Handshake e Negociação de Retomada

O protocolo inicia com uma negociação de status para permitir retomada de uma transferência interrompida.

| **Sinal** | **Descrição** |
|------------|----------------|
| `START:<filename>` | Início da transmissão, enviado pelo Emissor. |
| `ACK_STATUS:<block_id>` | Enviado pelo Receptor, indica o último bloco completo salvo (`.temp`). Se `0`, começa do início. |
| `END\n` | Enviado pelo Emissor ao final da transferência. |

---

### Lógica de Checkpointing (Retomada)

O **checkpointing** garante que interrupções (como `Ctrl+C` ou falhas de comunicação) não causem perda de progresso.

- **Emissor:**  
  Lê `block_id` do `ACK_STATUS` e usa `f_in.seek()` para retomar do ponto correto.

- **Receptor:**  
  Após cada ACK bem-sucedido, salva o ID do último bloco no arquivo temporário (`<filename>.temp`) via `save_checkpoint()`.

- **Finalização:**  
  Ao receber `END\n`, remove o arquivo `.temp` com `remove_checkpoint()`.

- **Interrupção:**  
  Caso ocorra, o arquivo `.temp` é mantido, permitindo retomada futura.

---

## 4. Estrutura de Código

| **Componente** | **Função** |
|------------------|------------|
| `calculate_crc32()` | Calcula o CRC32 (IEEE 802.3) para garantir integridade. |
| `emissor_handler()` | Gerencia envio: handshake, loop Stop-and-Wait, retentativas e finalização. |
| `receptor_handler()` | Gerencia recepção: aguarda `START:`, responde `ACK_STATUS`, verifica CRC/seq e grava no disco. |
| `save_checkpoint()` / `load_checkpoint()` | Manipulam o arquivo `.temp`, armazenando e lendo o índice do último bloco recebido. |

---

📡 **Resumo:**  
O protocolo implementa uma comunicação serial confiável e recuperável entre dois dispositivos, com controle de erro via **CRC32**, retransmissões automáticas via **Stop-and-Wait**, e **retomada automática** através de checkpointing persistente.
