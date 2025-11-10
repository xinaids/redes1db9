import serial
import time
import struct
import argparse
import os
import sys

# --- 1. Configurações Globais ---
BLOCK_SIZE = 100 
CRC_SIZE = 4     # 4 bytes para CRC e também para o Tamanho Real (UINT32)
START_SIGNAL = b'START:'
END_SIGNAL = b'END\n'

# --- 2. Camada de Enlace: Módulo CRC32 ---
POLYNOMIAL = 0xEDB88320
CRC_TABLE = []

def generate_crc_table():
    if CRC_TABLE:
        return
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ POLYNOMIAL if (crc & 1) else (crc >> 1)
        CRC_TABLE.append(crc)

def calculate_crc32(data: bytes) -> int:
    if not CRC_TABLE:
        generate_crc_table()
    crc = 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF

# --- 3. Funções de Comunicação ---

def emissor(file_path: str, port_name: str, baud_rate: int):
    """Função do Emissor: Envia o arquivo pela serial, usando padding e metadados de tamanho."""
    print(f"📡 EMISSOR | Porta: {port_name} | Baud: {baud_rate}")
    
    if not os.path.exists(file_path):
        print(f"❌ Erro: Arquivo '{file_path}' não encontrado.")
        return

    file_size = os.path.getsize(file_path)
    
    try:
        ser = serial.Serial(port_name, baud_rate, timeout=1)
        time.sleep(1) 

        with open(file_path, 'rb') as f:
            # 1. Camada de Aplicação: Enviar Cabeçalho de Início
            file_name_bytes = os.path.basename(file_path).encode('utf-8')
            ser.write(START_SIGNAL + file_name_bytes + b'\n')
            print(f"-> Iniciando transferência de: '{file_name_bytes.decode()}' ({file_size} bytes)")
            
            # Atraso crucial para socat/WSL: Permite que o Receptor processe o cabeçalho START
            time.sleep(0.5) 
            
            bytes_sent = 0
            while True:
                data_block = f.read(BLOCK_SIZE)
                if not data_block:
                    break

                real_size = len(data_block)
                
                # --- NOVIDADE: PADDING DO BLOCO ---
                # Garante que o bloco enviado tenha SEMPRE 100 bytes para sincronia
                if real_size < BLOCK_SIZE:
                    padding_needed = BLOCK_SIZE - real_size
                    data_block += b'\x00' * padding_needed
                
                # 2. Camada de Enlace: Calcula CRC32 no bloco preenchido
                checksum = calculate_crc32(data_block)
                checksum_bytes = struct.pack('<I', checksum)
                
                # NOVIDADE: Empacota o Tamanho Real do bloco (4 bytes)
                size_bytes = struct.pack('<I', real_size)
                
                # 3. Pacote: [CRC] + [Tamanho Real] + [Dados (100 bytes preenchidos)]
                packet = checksum_bytes + size_bytes + data_block
                ser.write(packet)
                bytes_sent += real_size # Contamos APENAS os bytes reais
                
                # Progresso no CMD
                sys.stdout.write(f"\r   Enviando... {bytes_sent / 1024:.2f} KB / {file_size / 1024:.2f} KB")
                sys.stdout.flush()
                
                time.sleep(0.005) # Controle de Fluxo Simplificado

            # 4. Enviar sinalizador de Fim
            ser.write(END_SIGNAL)
            print(f"\n✅ Transmissão concluída. Total de dados enviados: {bytes_sent} bytes.")

    except serial.SerialException as e:
        print(f"\n❌ Erro de comunicação serial ({port_name}): {e}")
    except Exception as e:
        print(f"\n❌ Ocorreu um erro inesperado: {e}")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

def receptor(port_name: str, baud_rate: int):
    """Função do Receptor: Lê serial, verifica CRC, descarta padding e salva o arquivo."""
    print(f"👂 RECEPTOR | Porta: {port_name} | Baud: {baud_rate}")
    
    ser = None
    output_file_handler = None
    
    try:
        ser = serial.Serial(port_name, baud_rate, timeout=3)
        
        # 1. Espera o cabeçalho START
        print("Aguardando cabeçalho de início (START:)...")
        file_name = None
        output_file = None
        while file_name is None:
            line = ser.readline()
            if line.startswith(START_SIGNAL):
                file_name = line[len(START_SIGNAL):].strip().decode('utf-8')
                output_file = "recebido_" + file_name
                print(f"-> Iniciando recepção. Arquivo de destino: {output_file}")
                output_file_handler = open(output_file, 'wb')
            elif line:
                 print(f"   Ignorando dado inesperado antes do START: {line.strip()}")
            
        bytes_received = 0
        error_count = 0
        
        # 2. Loop principal de recepção de blocos (108 bytes fixos)
        while True:
            # 2a. Leitura do CRC (4 bytes)
            checksum_bytes = ser.read(CRC_SIZE) 
            
            if checksum_bytes.startswith(b'E') and b'END' in checksum_bytes: 
                print("\n✅ Recebido sinalizador de FIM.")
                break
            
            # Se não leu 4 bytes, é erro de sincronia ou o buffer esvaziou
            if len(checksum_bytes) != CRC_SIZE:
                print("\n   ERRO GRAVE: Falha na leitura do CRC (Esperado 4 bytes).")
                break
            
            # 2b. Leitura do Tamanho Real (4 bytes)
            size_bytes = ser.read(CRC_SIZE) 
            if len(size_bytes) != CRC_SIZE:
                 print("\n   ERRO GRAVE: Falha na leitura do Tamanho Real (Esperado 4 bytes).")
                 break

            real_size = struct.unpack('<I', size_bytes)[0]
            
            # 2c. Leitura do Bloco de Dados (100 bytes fixos)
            data_block = ser.read(BLOCK_SIZE) 
            
            if len(data_block) != BLOCK_SIZE:
                 print(f"\n   ERRO DE SINCRONIZAÇÃO: Leu {len(data_block)} de {BLOCK_SIZE} bytes de dados. Desalinhamento total.")
                 error_count += 1
                 break # Erro irrecuperável

            # 3. Camada de Enlace: Verificação de Erros
            received_checksum = struct.unpack('<I', checksum_bytes)[0]
            calculated_checksum = calculate_crc32(data_block) # CRC calculado no bloco *preenchido*

            if received_checksum == calculated_checksum:
                # 4. Camada de Aplicação: Salva APENAS o número de bytes reais
                output_file_handler.write(data_block[:real_size])
                bytes_received += real_size
                
                # Progresso no CMD
                sys.stdout.write(f"\r   Recebendo... Total: {bytes_received / 1024:.2f} KB | Erros CRC: {error_count}")
                sys.stdout.flush()

            else:
                error_count += 1
                # Ignora este pacote inválido

        print(f"\nRecepção finalizada. Total de bytes válidos: {bytes_received}. Erros de CRC detectados: {error_count}.")

    except serial.SerialException as e:
        print(f"\n❌ Erro de comunicação serial ({port_name}): {e}")
    except Exception as e:
        print(f"\n❌ Ocorreu um erro inesperado: {e}")
    finally:
        if output_file_handler:
            output_file_handler.close()
            print(f"Arquivo de destino '{output_file}' fechado.")
        if ser and ser.is_open:
            ser.close()

def main():
    """Função principal para executar o Emissor ou Receptor via CMD."""
    parser = argparse.ArgumentParser(
        description="Protocolo de Transferência de Arquivos Serial (CMD Version).",
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