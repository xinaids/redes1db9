import serial
import time
import struct
import argparse
import os
import sys
import signal

# --- 1. Configurações Globais ---
BLOCK_SIZE = 100 
CRC_SIZE = 4
SEQ_SIZE = 1 # 1 byte para o número de sequência (0 ou 1)
# Tamanho máximo do pacote: SEQ (1) + CRC (4) + TamanhoReal (4) + Data (100) = 109 bytes
MAX_PACKET_SIZE = SEQ_SIZE + CRC_SIZE + 4 + BLOCK_SIZE 
MAX_FILENAME_LEN = 256

# --- Sinalizadores de Controle e Checkpointing ---
START_TRANSMISSION_SIGNAL = b'START:' # Emissor envia (START:<nome_arquivo>)
END_SIGNAL = b'END\n'                # Emissor envia (FIM)
ACK_STATUS_SIGNAL = b'ACK_STATUS:'   # Receptor responde (ACK_STATUS:<last_block_id>)
ACK_CHAR = b'A'                      # Confirmação Positiva de Pacote
NAK_CHAR = b'N'                      # Confirmação Negativa de Pacote

# --- Configuração do Protocolo Stop-and-Wait ---
TIMEOUT_SEC = 3 # Tempo de espera para o ACK em segundos
MAX_RETRANS = 5 # Número máximo de retransmissões antes de abortar

# --- Variáveis Globais de Sinal ---
received_interrupt = False

def signal_handler(signum, frame):
    """Trata o sinal de interrupção (Ctrl+C)."""
    global received_interrupt
    print("\n[PROTO] Sinal de interrupção (Ctrl+C) recebido.")
    received_interrupt = True

# --- 2. Camada de Enlace: Módulo CRC32 ---
POLYNOMIAL = 0xEDB88320
CRC_TABLE = []

def generate_crc_table():
    """Gera a tabela de CRC-32 para o polinômio 0xEDB88320 (IEEE 802.3)."""
    global CRC_TABLE
    if CRC_TABLE: return
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ POLYNOMIAL if (crc & 1) else (crc >> 1)
        CRC_TABLE.append(crc & 0xFFFFFFFF)

def calculate_crc32(data: bytes) -> bytes:
    """Calcula o CRC32 de um bloco de dados e retorna 4 bytes em formato little-endian."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ byte) & 0xFF]
    
    final_crc = crc ^ 0xFFFFFFFF
    # Retorna o CRC como 4 bytes no formato little-endian para o pacote
    return struct.pack('<I', final_crc)

# --- 3. Camada Física (Helper) ---

def receive_with_timeout(ser: serial.Serial, max_len: int, timeout_sec: int) -> bytes:
    """
    Tenta ler dados da porta serial com um tempo limite.
    É importante que o timeout da porta serial (ser.timeout) seja < timeout_sec.
    """
    # Ajusta o timeout interno para a leitura
    original_timeout = ser.timeout
    ser.timeout = timeout_sec
    
    start_time = time.time()
    data = b''
    
    while time.time() - start_time < timeout_sec:
        if received_interrupt:
            ser.timeout = original_timeout
            return b'' # Retorna vazio se Ctrl+C for pressionado
            
        try:
            # Tenta ler o restante dos dados
            chunk = ser.read(max_len - len(data))
            if chunk:
                data += chunk
                if len(data) >= max_len:
                    break
            else:
                # Se não leu nada, espera um pouco para evitar loop de CPU
                time.sleep(0.01)
        except serial.SerialException as e:
            print(f"[ERRO] Erro serial durante a leitura: {e}", file=sys.stderr)
            ser.timeout = original_timeout
            return b''
            
    ser.timeout = original_timeout
    return data

# --- Funções de Checkpointing ---

def get_checkpoint_filepath(filename: str) -> str:
    """Retorna o caminho do arquivo temporário de checkpoint."""
    return f"{filename}.temp"

def save_checkpoint(filename: str, last_block: int):
    """Salva o último bloco recebido com sucesso no arquivo .temp."""
    checkpoint_filepath = get_checkpoint_filepath(filename)
    try:
        with open(checkpoint_filepath, 'w') as f:
            f.write(str(last_block))
    except Exception as e:
        print(f"[ERRO] Falha ao salvar checkpoint: {e}", file=sys.stderr)

def load_checkpoint(filename: str) -> int:
    """Carrega o último bloco salvo, ou retorna 0."""
    checkpoint_filepath = get_checkpoint_filepath(filename)
    last_block = 0
    if os.path.exists(checkpoint_filepath):
        try:
            with open(checkpoint_filepath, 'r') as f:
                content = f.read().strip()
                last_block = int(content)
            print(f"[CHECKPOINT] Checkpoint encontrado. Retomando do Bloco {last_block + 1} (último recebido: {last_block}).")
        except Exception as e:
            print(f"[AVISO] Falha ao ler checkpoint, iniciando do zero: {e}", file=sys.stderr)
    return last_block

def remove_checkpoint(filename: str):
    """Remove o arquivo de checkpoint após conclusão bem-sucedida."""
    checkpoint_filepath = get_checkpoint_filepath(filename)
    if os.path.exists(checkpoint_filepath):
        os.remove(checkpoint_filepath)
        print("[CHECKPOINT] Arquivo de progresso temporário removido com sucesso.")

# --- 4. Camada de Aplicação/Controle: Funções do Protocolo ---

def emissor_handler(ser: serial.Serial, file_path: str):
    """Lógica do Emissor com Stop-and-Wait e Retomada."""
    global received_interrupt
    
    try:
        file_size = os.path.getsize(file_path)
        total_blocks = (file_size + BLOCK_SIZE - 1) // BLOCK_SIZE
        
        print(f"📡 EMISSOR | Tamanho: {file_size} bytes | Blocos Totais: {total_blocks}")
        
        # --- 1. Negociação de STATUS/Retomada ---
        
        # Envia sinal de STATUS/START
        status_signal = START_TRANSMISSION_SIGNAL + file_path.encode('utf-8') + b'\n'
        print(f"[PROTO] Enviando solicitação de STATUS/START para '{file_path}'...")
        
        current_block = 0
        retries = 0
        
        while retries < MAX_RETRANS:
            if received_interrupt: return
            
            ser.write(status_signal)
            
            # Aguarda resposta ACK_STATUS:X
            response = receive_with_timeout(ser, MAX_FILENAME_LEN, TIMEOUT_SEC)
            
            if response:
                if response.startswith(ACK_STATUS_SIGNAL):
                    try:
                        # Extrai o número do último bloco recebido pelo Receptor
                        block_str = response[len(ACK_STATUS_SIGNAL):].strip()
                        current_block = int(block_str.decode('utf-8'))
                        
                        # O Emissor começa a enviar do bloco *seguinte* ao último recebido
                        print(f"[PROTO] Recebido ACK de STATUS. Retomando do Bloco: {current_block + 1} (último recebido: {current_block}).")
                        break # Sai do loop de retentativas
                    except ValueError:
                        print(f"[ERRO] Resposta de STATUS inválida: {response}", file=sys.stderr)
                else:
                    print(f"[ERRO] Resposta inesperada: {response}. Reenviando STATUS.", file=sys.stderr)
            else:
                # Timeout
                retries += 1
                print(f"[TIMEOUT] Timeout ({retries}/{MAX_RETRANS}). Reenviando solicitação de STATUS/START...")

        if retries >= MAX_RETRANS:
            print("[ERRO] Máximo de retentativas de STATUS atingido. Abortando.")
            return

        # --- 2. Início da Transferência Stop-and-Wait ---
        
        current_block_to_send = current_block + 1
        current_seq_num = (current_block % 2) 
        if current_block == 0:
             current_seq_num = 0
        
        # Abre o arquivo para leitura no modo binário
        f_in = open(file_path, 'rb')
        f_in.seek(current_block * BLOCK_SIZE) # Move o ponteiro para o bloco de retomada

        print("[PROTO] Transferência Stop-and-Wait iniciada.")

        while current_block_to_send <= total_blocks:
            if received_interrupt:
                print("\n-- INTERRUPÇÃO RECEBIDA (Ctrl+C) -- Encerrando transmissão.")
                break
            
            # 2.1. Monta o Pacote
            data_buffer = f_in.read(BLOCK_SIZE)
            if not data_buffer and current_block_to_send <= total_blocks:
                 print("[ERRO] Falha na leitura do arquivo. Abortando.", file=sys.stderr)
                 break

            data_len = len(data_buffer)
            crc_bytes = calculate_crc32(data_buffer)

            # Estrutura do Pacote: SEQ (1 byte) + CRC (4 bytes) + Tamanho Real (4 bytes) + Dados (0-100 bytes)
            packet = bytes([current_seq_num]) + crc_bytes + struct.pack('<I', data_len) + data_buffer
            
            retries = 0
            ack_received = False

            while retries < MAX_RETRANS and not ack_received:
                if received_interrupt: break
                
                print(f"[EMISSOR] Enviando Bloco {current_block_to_send}/{total_blocks} (Seq: {current_seq_num}, CRC: {crc_bytes.hex()}). Tenta {retries + 1}/{MAX_RETRANS}.", end='\r')
                
                # 2.2. ENVIA O PACOTE
                ser.write(packet)
                
                # 2.3. AGUARDA ACK COM TIMEOUT
                response = receive_with_timeout(ser, 1, TIMEOUT_SEC)
                
                if response == ACK_CHAR:
                    print(f"[ACK] ACK Recebido. Bloco {current_block_to_send} confirmado. {' '*40}")
                    ack_received = True
                elif response == NAK_CHAR:
                    print(f"\n[NAK] NAK Recebido. Retransmitindo Bloco {current_block_to_send}.")
                    retries += 1
                elif response == b'':
                    # Timeout
                    retries += 1
                    print(f"\n[TIMEOUT] Timeout. Reenviando Bloco {current_block_to_send}.")
                else:
                    print(f"\n[ERRO] Resposta inesperada ({response}). Retransmitindo.", file=sys.stderr)
                    retries += 1
                    
            if received_interrupt: break
            
            if retries >= MAX_RETRANS:
                print(f"[ERRO] Máximo de retransmissões atingido para o Bloco {current_block_to_send}. Abortando.", file=sys.stderr)
                break
                
            if ack_received:
                # Prepara para o próximo bloco
                current_block_to_send += 1
                # Inverte o número de sequência (0 -> 1 ou 1 -> 0)
                current_seq_num = 1 - current_seq_num
            else:
                break

        # --- 3. Finalização ---
        if current_block_to_send > total_blocks and not received_interrupt:
            print("[PROTO] Transferência concluída. Enviando sinal END.")
            ser.write(END_SIGNAL)
        
        f_in.close()

    except FileNotFoundError:
        print(f"[ERRO] Arquivo '{file_path}' não encontrado.", file=sys.stderr)
    except serial.SerialException as e:
        print(f"[ERRO] Erro na porta serial: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[ERRO] Erro inesperado no Emissor: {e}", file=sys.stderr)
    finally:
        if ser.is_open:
            ser.close()
            print("Porta serial fechada.")

def receptor_handler(ser: serial.Serial):
    """Lógica do Receptor com Stop-and-Wait e Retomada."""
    global received_interrupt
    
    output_file_path = None
    f_out = None
    
    try:
        # 1. Aguarda Sinal de START/STATUS (Negociação)
        print("👂 RECEPTOR | Aguardando solicitação de STATUS do arquivo (máx 30 seg)...")
        
        # O Receptor espera pelo sinal START:nome_arquivo
        status_signal_received = receive_with_timeout(ser, MAX_FILENAME_LEN, 30)

        if not status_signal_received:
            print("[TIMEOUT] Timeout ao aguardar STATUS. Verifique a conexão do Emissor.")
            return

        if not status_signal_received.startswith(START_TRANSMISSION_SIGNAL):
            print(f"[ERRO] Sinal de STATUS inválido: {status_signal_received}", file=sys.stderr)
            return

        # Extrai o nome do arquivo
        output_file_path = status_signal_received[len(START_TRANSMISSION_SIGNAL):].strip().decode('utf-8')
        print(f"[PROTO] Recebido sinal de STATUS do arquivo '{output_file_path}'.")

        # 2. Lógica de Retomada e Resposta de STATUS
        last_block_received = load_checkpoint(output_file_path)
        
        # Abre o arquivo de destino: 'ab' para append binário, 'wb' se for do zero
        mode = 'ab' if last_block_received > 0 else 'wb'
        f_out = open(output_file_path, mode)
        
        # Responde ao Emissor de onde deve retomar
        ack_status_response = ACK_STATUS_SIGNAL + str(last_block_received).encode('utf-8') + b'\n'
        print(f"[PROTO] Enviando ACK de STATUS (Retomar do Bloco: {last_block_received + 1}).")
        ser.write(ack_status_response)

        # 3. Início da Recepção Stop-and-Wait
        
        expected_seq_num = (last_block_received % 2) 
        if last_block_received == 0:
             expected_seq_num = 0
             
        current_block = last_block_received
        
        print(f"[PROTO] Sequência esperada inicial: {expected_seq_num}")

        while True:
            if received_interrupt:
                print("\n-- INTERRUPÇÃO RECEBIDA (Ctrl+C) -- Arquivo preservado para retomada.")
                break
            
            # Aguarda o pacote de dados (Timeout longo para dar tempo ao Emissor de retransmitir)
            packet = receive_with_timeout(ser, MAX_PACKET_SIZE, 10) 
            
            if not packet:
                if received_interrupt: break
                print("\n[AVISO] Timeout na leitura de dados (Emissor pode ter parado). Encerrando recepção.")
                break

            # 3.1. Sinal de END
            if packet == END_SIGNAL:
                print("[PROTO] Sinal END recebido. Transferência concluída com sucesso.")
                remove_checkpoint(output_file_path)
                break
            
            # 3.2. Desmontagem e Verificação do Pacote
            if len(packet) < SEQ_SIZE + CRC_SIZE + 4:
                print(f"\n[ERRO] Pacote muito pequeno ({len(packet)} bytes). Enviando NAK.", file=sys.stderr)
                ser.write(NAK_CHAR)
                continue

            received_seq_num = packet[0]
            received_crc_bytes = packet[SEQ_SIZE : SEQ_SIZE + CRC_SIZE]
            
            # Extrai tamanho real dos dados e os dados
            data_len = struct.unpack('<I', packet[SEQ_SIZE + CRC_SIZE : SEQ_SIZE + CRC_SIZE + 4])[0]
            data_start = packet[SEQ_SIZE + CRC_SIZE + 4:]
            
            # Verifica se o tamanho do pacote recebido é consistente
            if len(data_start) != data_len or len(packet) != SEQ_SIZE + CRC_SIZE + 4 + data_len:
                print(f"\n[AVISO] Pacote com tamanho inconsistente (Data Len: {data_len}, Recebido: {len(data_start)}). Enviando NAK.", file=sys.stderr)
                ser.write(NAK_CHAR)
                continue
            
            # 3.3. Verificação de CRC
            calculated_crc_bytes = calculate_crc32(data_start)
            
            if calculated_crc_bytes != received_crc_bytes:
                print(f"\n[CRC-ERRO] Seq: {received_seq_num}. CRC esperado: {received_crc_bytes.hex()} | Calculado: {calculated_crc_bytes.hex()}. Enviando NAK.", file=sys.stderr)
                ser.write(NAK_CHAR)
                continue

            # 3.4. Verificação de Número de Sequência (Controle de Quadros Duplicados)
            if received_seq_num != expected_seq_num:
                # Se for o seq_num do *bloco anterior* (duplicado), enviamos ACK
                if received_seq_num == (1 - expected_seq_num):
                    print(f"[SEQ-AVISO] Bloco Duplicado (Seq: {received_seq_num}, Esperado: {expected_seq_num}). Reenviando ACK (Sem escrita).")
                    ser.write(ACK_CHAR)
                    continue
                else:
                    # Sequência totalmente inesperada
                    print(f"\n[SEQ-ERRO] Seq inesperada ({received_seq_num}, Esperado: {expected_seq_num}). Enviando NAK.", file=sys.stderr)
                    ser.write(NAK_CHAR)
                    continue

            # 3.5. Sucesso: Grava Dados e Envia ACK
            
            current_block += 1
            f_out.write(data_start)
            f_out.flush() # Força a escrita no disco para o checkpoint

            print(f"[RECEPTOR] Bloco {current_block} (Seq: {received_seq_num}) recebido e gravado. CRC OK. Enviando ACK. {' '*40}", end='\r')
            
            # 3.6. Envia ACK
            ser.write(ACK_CHAR)

            # Prepara para o próximo número de sequência esperado
            expected_seq_num = 1 - expected_seq_num
            
            # Salva Checkpoint (depois de gravar e enviar ACK)
            save_checkpoint(output_file_path, current_block)

    except serial.SerialException as e:
        print(f"[ERRO] Erro na porta serial: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[ERRO] Erro inesperado no Receptor: {e}", file=sys.stderr)
    finally:
        if f_out:
            f_out.close()
        if ser and ser.is_open:
            ser.close()
            print("\nPorta serial fechada.")

def main():
    # Define o handler para Ctrl+C antes de qualquer operação
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(
        description="Protocolo de Transferência de Arquivos Serial (Stop-and-Wait/PAR e Retomada).",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    parser.add_argument('modo', choices=['emissor', 'receptor'], 
                        help="Modo de operação: 'emissor' ou 'receptor'.")
    parser.add_argument('-p', '--port', type=str, required=True,
                        help="Porta serial a ser usada (Ex: /dev/ttyS0, COM3, /dev/pts/1).")
    parser.add_argument('-b', '--baud', type=int, default=115200,
                        help="Taxa de transmissão (default: 115200).") 
    parser.add_argument('-f', '--file', type=str, 
                        help="Caminho do arquivo a ser enviado (obrigatório para o modo emissor).")
    
    args = parser.parse_args()
    generate_crc_table() 
    
    ser = None
    try:
        # Configuração da porta serial (rtscts=True é recomendado para socat/Linux)
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1, # Timeout base para leitura de blocos de controle
            rtscts=True, # Controle de Fluxo por Hardware (RTS/CTS)
        )
        print(f"✅ Porta serial {args.port} aberta @ {args.baud} baud.")
        
        # Limpa o buffer antes de começar
        ser.flushInput()
        ser.flushOutput()

        if args.modo == 'emissor':
            if not args.file:
                parser.error("O modo 'emissor' requer o argumento '-f/--file'.")
            emissor_handler(ser, args.file)
        elif args.modo == 'receptor':
            receptor_handler(ser)
            
    except serial.SerialException as e:
        print(f"[ERRO FATAL] Não foi possível abrir a porta serial {args.port}: {e}", file=sys.stderr)
        print("Verifique se a porta existe e se não está sendo usada por outro processo (socat).", file=sys.stderr)
    except Exception as e:
        print(f"[ERRO] Ocorreu um erro inesperado: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
