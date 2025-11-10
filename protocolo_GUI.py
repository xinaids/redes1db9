import serial
import serial.tools.list_ports # NOVIDADE: Para listar portas
import time
import struct
import threading
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- 1. Configurações Globais ---
DEFAULT_PORT = '/dev/ttyS0' # Padrão Linux
DEFAULT_BAUD = 9600
BLOCK_SIZE = 100
CRC_SIZE = 4
# --- Sinalizadores de Controle ---
START_SIGNAL = b'START:'
END_SIGNAL = b'END\n'
ACK_SIGNAL = b'ACK\n'
NACK_SIGNAL = b'NACK\n'

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

# --- 3. Classe Principal da Interface Gráfica ---

class ProtocoloGUI:
    def __init__(self, master):
        self.master = master
        master.title("Protocolo Redes I - Serial (2, 3, 5)")
        
        self.serial_port = serial.Serial()
        self.running = False
        self.file_path = ""
        self.mode = tk.StringVar(value="Emissor")
        
        # Gera a tabela CRC uma vez no início
        generate_crc_table() 

        self._create_widgets()

    def _get_available_ports(self):
        """Lista as portas seriais disponíveis no sistema."""
        ports = serial.tools.list_ports.comports()
        # Retorna uma lista de strings contendo apenas o nome da porta (ex: '/dev/ttyS0')
        return [p.device for p in ports]

    def _refresh_ports(self):
        """Atualiza a lista de portas na Combobox."""
        new_ports = self._get_available_ports()
        self.port_entry['values'] = new_ports
        
        current_port = self.port_entry.get()
        if new_ports and current_port not in new_ports:
             # Tenta usar a porta padrão Linux, senão usa a primeira encontrada
             if DEFAULT_PORT in new_ports:
                 self.port_entry.set(DEFAULT_PORT)
             else:
                 self.port_entry.set(new_ports[0])
        elif not current_port:
             self.port_entry.set(DEFAULT_PORT)
             
        self.log("Lista de portas atualizada.")


    def _create_widgets(self):
        # Configurações Gerais
        frame_config = ttk.LabelFrame(self.master, text="🛠️ Configurações Gerais")
        frame_config.pack(padx=10, pady=10, fill="x")

        ttk.Label(frame_config, text="Porta Serial:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        
        # --- NOVIDADE: Combobox para Portas ---
        self.port_list = self._get_available_ports()
        self.port_entry = ttk.Combobox(frame_config, values=self.port_list, width=18)
        
        # Define o valor inicial
        if self.port_list:
            if DEFAULT_PORT in self.port_list:
                self.port_entry.set(DEFAULT_PORT)
            else:
                 self.port_entry.set(self.port_list[0])
        else:
            self.port_entry.set(DEFAULT_PORT)
            
        self.port_entry.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        
        # Botão para atualizar a lista
        self.refresh_button = ttk.Button(frame_config, text="⟳", width=3, command=self._refresh_ports)
        self.refresh_button.grid(row=0, column=2, padx=5, pady=5, sticky="e")
        
        # --- FIM NOVIDADE ---

        ttk.Label(frame_config, text="Baud Rate:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.baud_entry = ttk.Entry(frame_config, width=20)
        self.baud_entry.insert(0, str(DEFAULT_BAUD))
        self.baud_entry.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        # Modo de Operação
        frame_mode = ttk.LabelFrame(self.master, text="⚙️ Modo de Operação")
        frame_mode.pack(padx=10, pady=10, fill="x")

        self.radio_emissor = ttk.Radiobutton(frame_mode, text="Emissor (Enviar)", variable=self.mode, value="Emissor", command=self._update_ui)
        self.radio_emissor.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        
        self.radio_receptor = ttk.Radiobutton(frame_mode, text="Receptor (Receber)", variable=self.mode, value="Receptor", command=self._update_ui)
        self.radio_receptor.grid(row=0, column=1, padx=10, pady=5, sticky="w")

        # Controles do Emissor
        self.frame_emissor = ttk.LabelFrame(self.master, text="📤 Controle do Emissor")
        self.frame_emissor.pack(padx=10, pady=10, fill="x")
        
        self.file_label = ttk.Label(self.frame_emissor, text="Nenhum arquivo selecionado.")
        self.file_label.grid(row=0, column=0, padx=5, pady=5, sticky="w")
        
        self.file_button = ttk.Button(self.frame_emissor, text="Selecionar Arquivo", command=self._select_file)
        self.file_button.grid(row=0, column=1, padx=5, pady=5, sticky="e")
        
        # Botões de Ação
        frame_action = ttk.Frame(self.master)
        frame_action.pack(padx=10, pady=10, fill="x")
        
        self.start_button = ttk.Button(frame_action, text="INICIAR (Aguardar/Enviar)", command=self._start_stop)
        self.start_button.pack(side="left", fill="x", expand=True, padx=5)
        
        # Log e Progresso
        frame_log = ttk.LabelFrame(self.master, text="📊 Log e Progresso")
        frame_log.pack(padx=10, pady=10, fill="both", expand=True)
        
        self.progress_bar = ttk.Progressbar(frame_log, orient="horizontal", length=300, mode="determinate")
        self.progress_bar.pack(fill="x", padx=5, pady=5)
        
        self.log_text = tk.Text(frame_log, height=10, state='disabled')
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
        
        self._update_ui()
        self.log("Sistema pronto. Verifique as configurações da porta serial.")
        
    def _update_ui(self):
        if self.mode.get() == "Emissor":
            self.frame_emissor.pack(padx=10, pady=10, fill="x")
            self.start_button.config(text="INICIAR TRANSMISSÃO")
        else:
            self.frame_emissor.pack_forget()
            self.start_button.config(text="INICIAR ESCUTA (RECEPTOR)")

    def log(self, message):
        self.log_text.config(state='normal')
        self.log_text.insert('end', f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see('end')
        self.log_text.config(state='disabled')
        
    def _select_file(self):
        if self.running:
             messagebox.showerror("Erro", "Pare a operação antes de selecionar um novo arquivo.")
             return
             
        self.file_path = filedialog.askopenfilename()
        if self.file_path:
            self.file_label.config(text=f"Arquivo: {os.path.basename(self.file_path)} ({(os.path.getsize(self.file_path) / 1024):.2f} KB)")
            
    def _start_stop(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if self.mode.get() == "Emissor" and not self.file_path:
            messagebox.showerror("Erro", "Selecione um arquivo para enviar.")
            return

        try:
            port = self.port_entry.get()
            baud = int(self.baud_entry.get())
            
            self.serial_port = serial.Serial(port, baud, timeout=3)
            self.running = True
            
            self.port_entry.config(state='disabled')
            self.baud_entry.config(state='disabled')
            self.refresh_button.config(state='disabled')
            self.start_button.config(text="PARAR OPERAÇÃO", style='Danger.TButton')
            
            if self.mode.get() == "Emissor":
                thread = threading.Thread(target=self._run_emissor)
            else:
                thread = threading.Thread(target=self._run_receptor)
            
            thread.daemon = True
            thread.start()
            self.log(f"Comunicação iniciada em modo **{self.mode.get()}** na porta {port}.")
            
        except ValueError:
            messagebox.showerror("Erro", "Taxa de transmissão inválida.")
        except serial.SerialException as e:
            messagebox.showerror("Erro Serial", f"Não foi possível abrir a porta {self.port_entry.get()}.\nVerifique as permissões (grupo 'dialout') e se a porta está correta.\nErro: {e}")
            self.running = False

    def _stop(self):
        self.running = False
        try:
            if self.serial_port and self.serial_port.is_open:
                self.serial_port.close()
            self.log("Comunicação PARADA pelo usuário.")
        except Exception as e:
            self.log(f"Erro ao fechar a porta: {e}")
        finally:
            self.progress_bar['value'] = 0
            self.port_entry.config(state='normal')
            self.baud_entry.config(state='normal')
            self.refresh_button.config(state='normal')
            self.start_button.config(text="INICIAR (Aguardar/Enviar)", style='TButton')
            
    # --- Funções de Comunicação (Sem Alteração na Lógica de Protocolo) ---

    def _run_emissor(self):
        file_path = self.file_path
        file_size = os.path.getsize(file_path)
        
        try:
            with open(file_path, 'rb') as f:
                file_name_bytes = os.path.basename(file_path).encode('utf-8')
                self.serial_port.write(START_SIGNAL + file_name_bytes + b'\n')
                self.log(f"Enviando '{file_name_bytes.decode()}' ({file_size} bytes)...")
                
                bytes_sent = 0
                while self.running:
                    data_block = f.read(BLOCK_SIZE)
                    if not data_block:
                        break

                    checksum = calculate_crc32(data_block)
                    checksum_bytes = struct.pack('<I', checksum)
                    packet = checksum_bytes + data_block
                    
                    self.serial_port.write(packet)
                    bytes_sent += len(data_block)
                    
                    progress = (bytes_sent / file_size) * 100
                    self.master.after(0, lambda: self.progress_bar.config(value=progress))
                    
                    time.sleep(0.005) 
                    
                self.serial_port.write(END_SIGNAL)
                self.log(f"✅ Transmissão concluída. Total: {bytes_sent} bytes.")

        except Exception as e:
            self.log(f"❌ Erro durante a transmissão: {e}")
        finally:
            self._stop()

    def _run_receptor(self):
        output_file_handler = None
        
        try:
            self.log("Aguardando cabeçalho de início (START:)...")
            file_name = None
            while self.running and file_name is None:
                line = self.serial_port.readline()
                if line.startswith(START_SIGNAL):
                    file_name = line[len(START_SIGNAL):].strip().decode('utf-8')
                    output_file = "recebido_" + file_name
                    self.log(f"-> Recebido cabeçalho. Arquivo de destino: {output_file}")
                    output_file_handler = open(output_file, 'wb')
                elif line:
                     self.log(f"Ignorando dado inesperado: {line.strip()}")
                time.sleep(0.1)
                
            bytes_received = 0
            error_count = 0
            
            while self.running:
                checksum_bytes = self.serial_port.read(CRC_SIZE)
                
                if not checksum_bytes and self.serial_port.in_waiting == 0:
                    time.sleep(0.1)
                    continue
                    
                if checksum_bytes.startswith(b'E') and b'END' in checksum_bytes:
                    self.log("✅ Recebido sinalizador de FIM.")
                    break
                
                if len(checksum_bytes) != CRC_SIZE:
                    if checksum_bytes.startswith(b'E'):
                         self.serial_port.read_until(b'\n')
                         self.log("✅ Recebido sinalizador de FIM (corrigido).")
                         break
                    self.log("ERRO GRAVE: Falha na leitura do CRC.")
                    break
                    
                data_block = self.serial_port.read(BLOCK_SIZE)
                
                if len(data_block) != BLOCK_SIZE:
                    self.log(f"ERRO DE SINCRONIZAÇÃO: Leu {len(data_block)} de {BLOCK_SIZE} bytes.")
                    error_count += 1
                    continue

                received_checksum = struct.unpack('<I', checksum_bytes)[0]
                calculated_checksum = calculate_crc32(data_block)

                if received_checksum == calculated_checksum:
                    output_file_handler.write(data_block)
                    bytes_received += len(data_block)
                    # Apenas loga o progresso a cada 1KB para não poluir o log
                    if bytes_received % 1024 == 0:
                        self.master.after(0, lambda: self.log(f"   Recebido {bytes_received} bytes (OK)."))
                else:
                    error_count += 1
                    self.log(f"   ❌ ERRO DE DADOS detectado (CRC Inválido). Erros: {error_count}")

            self.log(f"Recepção finalizada. Total de bytes recebidos: {bytes_received}. Erros de CRC: {error_count}.")

        except Exception as e:
            self.log(f"❌ Erro durante a recepção: {e}")
        finally:
            if output_file_handler:
                output_file_handler.close()
                # O nome do arquivo pode ter sido criado, mas não preenchido em caso de erro.
                self.log("Arquivo de destino fechado.")
            self._stop()

# --- Execução Principal ---
if __name__ == '__main__':
    root = tk.Tk()
    style = ttk.Style(root)
    # Garante que o tema GTK (nativo do Linux Mint) seja usado
    style.theme_use('clam') 
    style.configure('Danger.TButton', foreground='red')
    
    app = ProtocoloGUI(root)
    root.mainloop()