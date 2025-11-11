import serial
import time
import struct
import argparse
import os
import sys
import signal

# --- Configurações Globais ---
BLOCK_SIZE = 100
CRC_SIZE = 4
SEQ_SIZE = 1
MAX_PACKET_SIZE = SEQ_SIZE + CRC_SIZE + 4 + BLOCK_SIZE
MAX_FILENAME_LEN = 256

# --- Sinais de Controle ---
START_TRANSMISSION_SIGNAL = b'START:'
END_SIGNAL = b'END\n'
ACK_STATUS_SIGNAL = b'ACK_STATUS:'
ACK_CHAR = b'A'
NAK_CHAR = b'N'

# --- Parâmetros do Protocolo ---
TIMEOUT_SEC = 3
MAX_RETRANS = 5

received_interrupt = False


def signal_handler(signum, frame):
    global received_interrupt
    print("\n[PROTO] Sinal de interrupção (Ctrl+C) recebido.")
    received_interrupt = True


# --- CRC32 ---
POLYNOMIAL = 0xEDB88320
CRC_TABLE = []


def generate_crc_table():
    global CRC_TABLE
    if CRC_TABLE:
        return
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ POLYNOMIAL if (crc & 1) else (crc >> 1)
        CRC_TABLE.append(crc & 0xFFFFFFFF)


def calculate_crc32(data: bytes) -> bytes:
    crc = 0xFFFFFFFF
    for byte in data:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ byte) & 0xFF]
    return struct.pack('<I', crc ^ 0xFFFFFFFF)


# --- Funções Auxiliares ---
def receive_with_timeout(ser: serial.Serial, max_len: int, timeout_sec: int) -> bytes:
    """Lê dados da porta com timeout"""
    original_timeout = ser.timeout
    ser.timeout = timeout_sec
    data = b''
    start_time = time.time()

    while time.time() - start_time < timeout_sec:
        if received_interrupt:
            ser.timeout = original_timeout
            return b''
        chunk = ser.read(max_len - len(data))
        if chunk:
            data += chunk
            if len(data) >= max_len:
                break
        else:
            time.sleep(0.01)
    ser.timeout = original_timeout
    return data


def get_checkpoint_filepath(filename: str) -> str:
    return f"{filename}.temp"


def save_checkpoint(filename: str, last_block: int):
    try:
        with open(get_checkpoint_filepath(filename), 'w') as f:
            f.write(str(last_block))
    except Exception as e:
        print(f"[ERRO] Falha ao salvar checkpoint: {e}", file=sys.stderr)


def load_checkpoint(filename: str) -> int:
    path = get_checkpoint_filepath(filename)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return 0


def remove_checkpoint(filename: str):
    path = get_checkpoint_filepath(filename)
    if os.path.exists(path):
        os.remove(path)
        print("[CHECKPOINT] Removido com sucesso.")


# --- Emissor ---
def emissor_handler(ser: serial.Serial, file_path: str):
    global received_interrupt
    try:
        file_size = os.path.getsize(file_path)
        total_blocks = (file_size + BLOCK_SIZE - 1) // BLOCK_SIZE
        print(f"📡 EMISSOR | Tamanho: {file_size} bytes | Blocos Totais: {total_blocks}")

        # Handshake inicial
        status_signal = START_TRANSMISSION_SIGNAL + file_path.encode('utf-8') + b'\n'
        print(f"[PROTO] Enviando solicitação de STATUS/START para '{file_path}'...")

        current_block = 0
        retries = 0
        while retries < MAX_RETRANS:
            if received_interrupt:
                return
            ser.write(status_signal)
            response = receive_with_timeout(ser, MAX_FILENAME_LEN, TIMEOUT_SEC)
            if response and response.startswith(ACK_STATUS_SIGNAL):
                try:
                    blk = int(response[len(ACK_STATUS_SIGNAL):].strip().decode('utf-8'))
                    current_block = blk
                    print(f"[PROTO] Recebido ACK de STATUS. Retomando do Bloco {current_block}.")
                    break
                except Exception:
                    pass
            retries += 1
            print(f"[TIMEOUT] Timeout ({retries}/{MAX_RETRANS}). Reenviando solicitação...")
        if retries >= MAX_RETRANS:
            print("[ERRO] Máximo de retentativas atingido. Abortando.")
            return

        # Envio dos dados
        current_block_to_send = current_block
        current_seq_num = current_block % 2

        with open(file_path, 'rb') as f_in:
            f_in.seek(current_block * BLOCK_SIZE)
            print("[PROTO] Transferência Stop-and-Wait iniciada.")

            while current_block_to_send <= total_blocks - 1:
                if received_interrupt:
                    print("\n-- INTERRUPÇÃO RECEBIDA --")
                    break

                data_buffer = f_in.read(BLOCK_SIZE)
                if not data_buffer:
                    break

                data_len = len(data_buffer)
                crc_bytes = calculate_crc32(data_buffer)
                packet = bytes([current_seq_num]) + crc_bytes + struct.pack('<I', data_len) + data_buffer

                retries = 0
                ack_ok = False
                while retries < MAX_RETRANS and not ack_ok:
                    ser.write(packet)
                    response = receive_with_timeout(ser, 1, TIMEOUT_SEC)
                    if response == ACK_CHAR:
                        print(f"[ACK] Bloco {current_block_to_send + 1} confirmado.")
                        ack_ok = True
                    elif response == NAK_CHAR:
                        print(f"[NAK] Retransmitindo Bloco {current_block_to_send + 1}.")
                        retries += 1
                    else:
                        retries += 1
                        print(f"[TIMEOUT] Sem resposta, reenviando Bloco {current_block_to_send + 1}.")

                if not ack_ok:
                    print(f"[ERRO] Falha no Bloco {current_block_to_send + 1}. Abortando.")
                    break

                current_block_to_send += 1
                current_seq_num = 1 - current_seq_num

        if current_block_to_send >= total_blocks and not received_interrupt:
            print("[PROTO] Transferência concluída. Enviando END.")
            ser.write(END_SIGNAL)

    except Exception as e:
        print(f"[ERRO] {e}", file=sys.stderr)
    finally:
        if ser.is_open:
            ser.close()
            print("Porta serial fechada.")


# --- Receptor ---
def receptor_handler(ser: serial.Serial):
    global received_interrupt
    try:
        print("👂 RECEPTOR | Aguardando solicitação de STATUS do arquivo (máx 30 seg)...")

        ser.timeout = 30
        status_signal_received = ser.readline()
        if not status_signal_received:
            print("[TIMEOUT] Timeout ao aguardar STATUS.")
            return

        ser.flushInput()
        ser.flushOutput()

        if not status_signal_received.startswith(START_TRANSMISSION_SIGNAL):
            print(f"[ERRO] Sinal inválido: {status_signal_received}")
            return

        file_name = status_signal_received[len(START_TRANSMISSION_SIGNAL):].strip().decode('utf-8')
        base_name = os.path.basename(file_name)
        output_file_path = f"recebido_{base_name}"
        print(f"[PROTO] Recebido sinal de STATUS do arquivo '{file_name}'. Será salvo como '{output_file_path}'.")

        last_block_received = load_checkpoint(output_file_path)
        mode = 'ab' if last_block_received > 0 else 'wb'
        f_out = open(output_file_path, mode)

        ack_status = ACK_STATUS_SIGNAL + str(last_block_received).encode('utf-8') + b'\n'
        ser.write(ack_status)
        print(f"[PROTO] Enviando ACK_STATUS (Retomar do Bloco {last_block_received}).")

        expected_seq_num = last_block_received % 2
        current_block = last_block_received

        while True:
            # leitura robusta
            header = receive_with_timeout(ser, 1, 10)
            if not header:
                print("[AVISO] Timeout de leitura. Encerrando recepção.")
                break

            header_rest = receive_with_timeout(ser, 8, 1)
            if len(header_rest) < 8:
                ser.write(NAK_CHAR)
                continue

            seq = header[0]
            recv_crc = header_rest[0:4]
            data_len = struct.unpack('<I', header_rest[4:8])[0]

            data = receive_with_timeout(ser, data_len, 2)
            if len(data) != data_len:
                ser.write(NAK_CHAR)
                continue

            calc_crc = calculate_crc32(data)
            if calc_crc != recv_crc:
                ser.write(NAK_CHAR)
                continue

            if seq != expected_seq_num:
                if seq == (1 - expected_seq_num):
                    ser.write(ACK_CHAR)
                    continue
                else:
                    ser.write(NAK_CHAR)
                    continue

            f_out.write(data)
            f_out.flush()
            ser.write(ACK_CHAR)
            expected_seq_num = 1 - expected_seq_num
            current_block += 1
            save_checkpoint(output_file_path, current_block)
            print(f"[RECEPTOR] Bloco {current_block} OK. Enviando ACK.")

        f_out.close()
        print("[PROTO] Sinal END recebido. Transferência concluída.")
        remove_checkpoint(output_file_path)

    except Exception as e:
        print(f"[ERRO] {e}", file=sys.stderr)
    finally:
        if ser.is_open:
            ser.close()
            print("Porta serial fechada.")


# --- Main ---
def main():
    signal.signal(signal.SIGINT, signal_handler)
    parser = argparse.ArgumentParser()
    parser.add_argument('modo', choices=['emissor', 'receptor'])
    parser.add_argument('-p', '--port', required=True)
    parser.add_argument('-b', '--baud', type=int, default=115200)
    parser.add_argument('-f', '--file')
    args = parser.parse_args()

    generate_crc_table()

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1,
            rtscts=False,
        )
        print(f"✅ Porta serial {args.port} aberta @ {args.baud} baud.")
        ser.flushInput()
        ser.flushOutput()

        if args.modo == 'emissor':
            if not args.file:
                parser.error("O modo 'emissor' requer '-f/--file'.")
            emissor_handler(ser, args.file)
        else:
            receptor_handler(ser)

    except Exception as e:
        print(f"[ERRO FATAL] {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
