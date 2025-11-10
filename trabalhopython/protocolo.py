import serial
import time
import struct
import argparse
import os
import sys

# --- 1. Configurações Globais ---
BLOCK_SIZE = 100 
CRC_SIZE = 4
# --- Sinalizadores de Retomada (Checkpointing) ---
FILE_STATUS_SIGNAL = b'STATUS:'
ACK_POS_SIGNAL = b'ACK_POS:'
START_NEW_SIGNAL = b'START_NEW\n'
START_TRANSMISSION_SIGNAL = b'START:' # Sinal de início antes do loop de envio de blocos
END_SIGNAL = b'END\n'

# --- 2. Camada de Enlace: Módulo CRC32 ---
POLYNOMIAL = 0xEDB88320
CRC_TABLE = []

def generate_crc_table():
    if CRC_TABLE: return
    for i in range(256):
        crc = i
        for _ in range(8):
            # Esta linha garante o cálculo correto do CRC32
            crc = (crc >> 1) ^ POLYNOMIAL if (crc & 1) else (crc >> 1)
        CRC_TABLE.append(crc)

def calculate_crc32(data: bytes) -> int:
    if not CRC_TABLE: generate_crc_table()
    crc = 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ byte) & 0xFF]
    # Retorna o complemento de 2 do resultado final
    return crc ^ 0xFFFFFFFF

# --- 3. Funções de Comunicação ---

def emissor(file_path: str, port_name: str, baud_rate: int):
    """Função do Emissor: Implementa Handshake de Retomada e Envio."""
    print(f"📡 EMISSOR | Porta: {port_name} | Baud: {baud_rate}")
    
    if not os.path.exists(file_path):
        print(f"❌ Erro: Arquivo '{file_path}' não encontrado.")
        return

    file_name = os.path.basename(file_path)
    file_name_bytes = file_name.encode('utf-8')
    file_size = os.path.getsize(file_path)
    
    ser = None
    
    try:
        # Habilita o Controle de Fluxo por Hardware (RTS/CTS)
        ser = serial.Serial(port_name, baud_rate, timeout=10, rtscts=True) 
        time.sleep(1) 
        # Limpa o buffer de entrada
        ser.reset_input_buffer() 
        

        with open(file_path, 'rb') as f:
            
            # --- 1. HANDSHAKE DE RETOMADA (Camada de Aplicação) ---
            start_offset = 0
            
            # Envia a solicitação de status do arquivo
            request_message = FILE_STATUS_SIGNAL + file_name_bytes + b'\n'
            ser.write(request_message)
            print(f"-> Solicitando STATUS do arquivo '{file_name}'...")
            
            time.sleep(0.5) 
            response = ser.readline().strip() 

            if response.startswith(ACK_POS_SIGNAL):
                try:
                    offset_str = response[len(ACK_POS_SIGNAL):].decode('utf-8')
                    resume_offset = int(offset_str)
                    
                    if resume_offset > 0 and resume_offset < file_size:
                        start_offset = resume_offset
                        f.seek(start_offset) # Pula bytes no arquivo de origem
                        print(f"-> **RETOMADA SOLICITADA** a partir do byte: {start_offset} ({start_offset / file_size * 100:.2f}%)")
                    elif resume_offset >= file_size:
                        print(f"-> ✅ Arquivo já está COMPLETO no Receptor. Parando.")
                        return 
                    else:
                        print("-> Retomada inválida (Offset 0). Iniciando do zero.")
                except (ValueError, UnicodeDecodeError):
                    print("-> Resposta ACK_POS inválida. Iniciando do zero.")
            
            elif response == START_NEW_SIGNAL.strip():
                print("-> Receptor solicitou início de uma nova transferência.")
                
            else:
                print(f"-> Resposta inesperada ou vazia. Iniciando do zero. ({response.decode() if response else 'vazia'})")

            # Volta o timeout para o normal
            ser.timeout = 1 
            
            # --- 2. LOOP DE TRANSMISSÃO ---
            bytes_sent = start_offset
            
            # Envia o sinal 'START:' que o Receptor espera para iniciar o loop de bytes.
            ser.write(START_TRANSMISSION_SIGNAL + file_name_bytes + b'\n')
            
            while True:
                data_block = f.read(BLOCK_SIZE)
                if not data_block:
                    break

                real_size = len(data_block)
                
                # PADDING DO BLOCO: Garante que o bloco tenha sempre BLOCK_SIZE bytes para o cálculo do CRC
                if real_size < BLOCK_SIZE:
                    padding_needed = BLOCK_SIZE - real_size
                    data_block += b'\x00' * padding_needed
                
                # Camada de Enlace: Calcula CRC32 e empacota Tamanho Real
                checksum = calculate_crc32(data_block)
                checksum_bytes = struct.pack('<I', checksum)
                size_bytes = struct.pack('<I', real_size)
                
                # Pacote: [CRC (4)] + [Tamanho Real (4)] + [Dados (100 bytes preenchidos)]
                packet = checksum_bytes + size_bytes + data_block
                ser.write(packet)
                bytes_sent += real_size
                
                # Atualização do progresso
                sys.stdout.write(f"\r   Enviando... {bytes_sent / 1024:.2f} KB / {file_size / 1024:.2f} KB | Progresso: {bytes_sent / file_size * 100:.2f}%")
                sys.stdout.flush()
                
                # Pequena pausa para controle de fluxo (mesmo com RTS/CTS, ajuda)
                time.sleep(0.005) 

            # 3. Enviar sinalizador de Fim
            ser.write(END_SIGNAL)
            print(f"\n✅ Transmissão concluída. Total de dados enviados: {bytes_sent} bytes.")

    except serial.SerialException as e:
        print(f"\n❌ Erro de comunicação serial ({port_name}): {e}")
    except KeyboardInterrupt:
        print("\n\n-- INTERRUPÇÃO RECEBIDA (Ctrl+C) -- Programa encerrado.")
    except Exception as e:
        print(f"\n❌ Ocorreu um erro inesperado: {e}")
    finally:
        if ser and ser.is_open:
            print("Porta serial fechada.")
            ser.close()

def receptor(port_name: str, baud_rate: int):
    """Função do Receptor: Implementa Handshake de Retomada e Recebimento."""
    print(f"👂 RECEPTOR | Porta: {port_name} | Baud: {baud_rate}")
    
    ser = None
    output_file_handler = None
    current_file_path = None 
    END_SIGNAL_RECEIVED = False # Flag para controle de interrupção
    
    try:
        # Habilita o Controle de Fluxo por Hardware (RTS/CTS)
        ser = serial.Serial(port_name, baud_rate, timeout=10, rtscts=True)
        # Limpa o buffer de entrada
        ser.reset_input_buffer()
        
        # 1. Espera o cabeçalho de STATUS
        print("Aguardando solicitação de STATUS do arquivo...")
        file_name = None
        
        # Loop robusto para encontrar o sinal STATUS:
        while file_name is None:
            # Aumenta o timeout para o handshake inicial
            ser.timeout = 30 
            line = ser.readline()
            ser.timeout = 10 # Volta o timeout
            
            if line.startswith(FILE_STATUS_SIGNAL):
                file_name = line[len(FILE_STATUS_SIGNAL):].strip().decode('utf-8')
            elif line:
                print(f"   Ignorando dado inesperado antes do STATUS: {line.strip()}")
            else: 
                print("Timeout ao aguardar STATUS. Verifique a conexão do Emissor.")
                return

        # --- 2. LÓGICA DE CHECKPOINTING/RESUME ---
        output_file = "recebido_" + file_name
        current_file_path = os.path.join(os.getcwd(), output_file)

        resume_offset = 0
        file_mode = 'wb'
        response_signal = START_NEW_SIGNAL
        
        # Verifica se o arquivo já existe para retomar
        if os.path.exists(current_file_path):
            try:
                file_size_local = os.path.getsize(current_file_path)
                
                if file_size_local > 0: 
                    resume_offset = file_size_local
                    file_mode = 'ab' # Abre em modo append para continuar de onde parou
                    # Monta o sinal ACK_POS: com o offset atual para o Emissor
                    response_signal = ACK_POS_SIGNAL + str(resume_offset).encode('utf-8') + b'\n'
                    print(f"-> Arquivo EXISTE. Solicitando retomada do byte: {resume_offset}")
                else:
                    print("-> Arquivo existe, mas está vazio. Reiniciando a transferência.")
            except Exception as e:
                print(f"-> Erro ao verificar arquivo local. Reiniciando. Erro: {e}")

        # Envia sinal de resposta ao Emissor (ACK_POS ou START_NEW)
        ser.write(response_signal)
        time.sleep(0.1) 
        
        output_file_handler = open(output_file, file_mode)
        
        # Valida o sinal START: antes de iniciar o loop de bytes
        print("-> Aguardando sinal de INÍCIO da Transmissão...")
        ser.timeout = 3 
        
        start_ack_line = ser.readline()
        if not start_ack_line.startswith(START_TRANSMISSION_SIGNAL):
            print(f"❌ ERRO GRAVE: Sinal de INÍCIO da Transmissão (START:) não recebido. Linha: {start_ack_line.strip()}")
            return 
        
        bytes_received = resume_offset
        error_count = 0
        
        # 3. LOOP PRINCIPAL DE RECEPÇÃO DE BLOCOS (108 bytes fixos)
        while True:
            # Tenta ler o CRC (4 bytes)
            checksum_bytes = ser.read(CRC_SIZE) 
            
            # --- Tratamento de Sinal de FIM/Timeout ---
            if not checksum_bytes:
                print("\n   Timeout na leitura de bloco. Transmissão interrompida ou encerrada inesperadamente.")
                break
            
            # Checa se o que foi lido (mesmo parcial) contém o sinal de FIM.
            if END_SIGNAL.strip() in checksum_bytes: 
                # Tenta ler o resto do sinal para limpar o buffer, caso haja algo mais.
                ser.read(len(END_SIGNAL) - len(checksum_bytes))
                END_SIGNAL_RECEIVED = True 
                print("\n✅ Recebido sinalizador de FIM.")
                break
                
            # Se não é o sinal de FIM, mas a leitura do CRC foi curta (erro de sincronização)
            if len(checksum_bytes) != CRC_SIZE:
                print(f"\n   ERRO GRAVE: Falha na leitura do CRC (Esperado {CRC_SIZE} bytes, leu {len(checksum_bytes)}). Desalinhamento.")
                break
            
            # Leitura do Tamanho Real (4 bytes)
            size_bytes = ser.read(CRC_SIZE) 
            if len(size_bytes) != CRC_SIZE:
                print(f"\n   ERRO GRAVE: Falha na leitura do Tamanho Real (Esperado {CRC_SIZE} bytes, leu {len(size_bytes)}). Desalinhamento.")
                break
                
            # Leitura dos Dados (100 bytes)
            real_size = struct.unpack('<I', size_bytes)[0]
            data_block = ser.read(BLOCK_SIZE) 
            
            if len(data_block) != BLOCK_SIZE:
                print(f"\n   ERRO DE SINCRONIZAÇÃO: Leu {len(data_block)} de {BLOCK_SIZE} bytes de dados. Desalinhamento total.")
                error_count += 1
                break 

            # Desempacota e calcula o CRC
            received_checksum = struct.unpack('<I', checksum_bytes)[0]
            calculated_checksum = calculate_crc32(data_block) 

            # Validação e Escrita
            if received_checksum == calculated_checksum:
                # Escreve APENAS o real_size de bytes válidos no arquivo
                output_file_handler.write(data_block[:real_size])
                bytes_received += real_size
                
                sys.stdout.write(f"\r   Recebendo... Total: {bytes_received / 1024:.2f} KB | Erros CRC: {error_count}")
                sys.stdout.flush()

            else:
                error_count += 1
                # O protocolo de Enlace não tenta retransmissão, apenas conta o erro.

        print(f"\nRecepção finalizada. Total de bytes válidos: {bytes_received}. Erros de CRC detectados: {error_count}.")

    except serial.SerialException as e:
        print(f"\n❌ Erro de comunicação serial ({port_name}): {e}")
        
    except KeyboardInterrupt:
        print("\n\n-- INTERRUPÇÃO RECEBIDA (Ctrl+C) -- Arquivo preservado para retomada.")

    except Exception as e:
        print(f"\n❌ Ocorreu um erro inesperado: {e}")
    
    finally:
        # Lógica de limpeza corrigida: Preserva o arquivo parcial para permitir a retomada
        if output_file_handler:
            output_file_handler.close()
            if END_SIGNAL_RECEIVED:
                print(f"Arquivo de destino '{output_file}' FINALIZADO e FECHADO.")
            elif current_file_path and os.path.exists(current_file_path):
                 print(f"Arquivo de destino '{output_file}' FECHADO. Está PARCIALMENTE COMPLETO para retomada.")
            
        if ser and ser.is_open:
            ser.close()
            
def main():
    parser = argparse.ArgumentParser(
        description="Protocolo de Transferência de Arquivos Serial (CMD Version - com Retomada Robustizada e Flow Control HW).",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    parser.add_argument('modo', choices=['emissor', 'receptor'], 
                        help="Modo de operação: 'emissor' ou 'receptor'.")
    parser.add_argument('-p', '--port', type=str, required=True,
                        help="Porta serial a ser usada (Ex: /dev/ttyS0, COM3, /dev/pts/1).")
    parser.add_argument('-b', '--baud', type=int, default=9600,
                        help="Taxa de transmissão (default: 9600).")
    parser.add_argument('-f', '--file', type=str, 
                        help="Caminho do arquivo a ser enviado (obrigatório para o modo emissor).")
    
    args = parser.parse_args()
    generate_crc_table() 
    
    if args.modo == 'emissor':
        if not args.file:
            parser.error("O modo 'emissor' requer o argumento -f/--file.")
        emissor(args.file, args.port, args.baud)
        
    elif args.modo == 'receptor':
        receptor(args.port, args.baud)

if __name__ == '__main__':
    main()