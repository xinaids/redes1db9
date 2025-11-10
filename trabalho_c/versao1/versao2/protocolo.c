#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>

// --- 1. Configurações Globais ---
#define BLOCK_SIZE 100 
#define CRC_SIZE 4
#define PACKET_SIZE (CRC_SIZE + sizeof(uint32_t) + BLOCK_SIZE) // 4 (CRC) + 4 (Real Size) + 100 (Data) = 108 bytes
#define MAX_FILENAME_LEN 256

// --- Sinalizadores de Retomada (Checkpointing) ---
#define FILE_STATUS_SIGNAL "STATUS:"
#define ACK_POS_SIGNAL "ACK_POS:"
#define START_NEW_SIGNAL "START_NEW\n"
#define START_TRANSMISSION_SIGNAL "START:" // Sinal de início antes do loop de envio de blocos
#define END_SIGNAL "END\n"

// --- 2. Camada de Enlace: Módulo CRC32 ---
#define POLYNOMIAL 0xEDB88320
uint32_t crc_table[256] = {0};

/**
 * @brief Gera a tabela de CRC32.
 */
void generate_crc_table() {
    if (crc_table[1] != 0) return; // Já gerada
    uint32_t i, j, crc;
    for (i = 0; i < 256; ++i) {
        crc = i;
        for (j = 0; j < 8; ++j) {
            // Garante o cálculo correto do CRC32 (complemento de 2 do divisor)
            crc = (crc & 1) ? (crc >> 1) ^ POLYNOMIAL : (crc >> 1);
        }
        crc_table[i] = crc;
    }
}

/**
 * @brief Calcula o CRC32 (IEEE 802.3) de um bloco de dados.
 * @param data Ponteiro para o bloco de dados (BLOCO PREENCHIDO).
 * @param length Tamanho do bloco (BLOCK_SIZE, 100).
 * @return O valor do CRC32.
 */
uint32_t calculate_crc32(const unsigned char *data, size_t length) {
    if (crc_table[1] == 0) generate_crc_table();
    uint32_t crc = 0xFFFFFFFF; // Valor inicial
    size_t i;

    for (i = 0; i < length; ++i) {
        // Combina o byte de entrada com o byte menos significativo do CRC atual
        crc = (crc >> 8) ^ crc_table[(crc ^ data[i]) & 0xFF];
    }

    // Retorna o complemento de 2 (XOR com 0xFFFFFFFF)
    return crc ^ 0xFFFFFFFF;
}

// --- 3. Configuração da Porta Serial (POSIX) ---

/**
 * @brief Configura e abre a porta serial.
 * @param port_name Nome da porta (ex: /dev/ttyS0).
 * @param baud_rate Taxa de transmissão.
 * @return File Descriptor da porta serial, ou -1 em caso de erro.
 */
int setup_serial_port(const char *port_name, int baud_rate) {
    int fd = open(port_name, O_RDWR | O_NOCTTY | O_NDELAY);
    if (fd < 0) {
        perror("Erro ao abrir a porta serial");
        return -1;
    }

    struct termios tty;
    if (tcgetattr(fd, &tty) != 0) {
        perror("Erro ao ler configurações da porta serial");
        close(fd);
        return -1;
    }

    // Configurações de controle
    tty.c_cflag |= (CLOCAL | CREAD); // Ignora linhas de controle de modem, Habilita recepção
    tty.c_cflag &= ~PARENB;          // Sem paridade
    tty.c_cflag &= ~CSTOPB;          // 1 bit de parada
    tty.c_cflag &= ~CSIZE;
    tty.c_cflag |= CS8;              // 8 bits de dados

    // Controle de Fluxo por Hardware (RTS/CTS)
    // O código Python explicitamente habilitava rtscts=True.
    // Em POSIX, é CRTSCTS
    tty.c_cflag |= CRTSCTS; 

    // Configurações de entrada (RAW mode)
    tty.c_lflag = 0;                 // Sem processamento canônico
    tty.c_oflag = 0;                 // Sem processamento de saída

    // Configurações de timeout (VMIN=1, VTIME=0 para leitura bloqueante de 1 byte ou mais)
    // Para non-blocking (como o Python com timeout=10 no receptor), usaremos VMIN/VTIME para timeout
    tty.c_cc[VMIN] = 0;     // Mínimo de caracteres a ler
    tty.c_cc[VTIME] = 50;   // Timeout de 5 segundos (50 * 0.1s)

    // Configura taxa de transmissão
    speed_t speed;
    switch (baud_rate) {
        case 9600: speed = B9600; break;
        case 115200: speed = B115200; break;
        default:
            fprintf(stderr, "Taxa de transmissão (%d) não suportada.\n", baud_rate);
            close(fd);
            return -1;
    }
    cfsetispeed(&tty, speed);
    cfsetospeed(&tty, speed);

    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("Erro ao aplicar configurações da porta serial");
        close(fd);
        return -1;
    }

    tcflush(fd, TCIOFLUSH); // Limpa buffers de entrada/saída
    return fd;
}

/**
 * @brief Lê uma linha (até \n) da porta serial.
 * @param fd File Descriptor.
 * @param buffer Buffer de destino.
 * @param max_len Tamanho máximo do buffer.
 * @return Número de bytes lidos (0 em timeout ou -1 em erro).
 */
ssize_t serial_readline(int fd, char *buffer, size_t max_len) {
    size_t i = 0;
    char c;
    
    // Configura um timeout mais longo para o handshake inicial (como o Python fazia)
    struct termios tty;
    if (tcgetattr(fd, &tty) == 0) {
        tty.c_cc[VTIME] = 100; // 10 segundos para o handshake
        tcsetattr(fd, TCSANOW, &tty);
    }

    while (i < max_len - 1) {
        ssize_t n = read(fd, &c, 1);
        if (n > 0) {
            buffer[i++] = c;
            if (c == '\n') {
                break;
            }
        } else if (n == 0) {
            // Timeout
            break;
        } else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
            perror("Erro de leitura serial");
            return -1;
        }
    }
    buffer[i] = '\0';

    // Restaura o timeout mais curto para o loop de blocos
    if (tcgetattr(fd, &tty) == 0) {
        tty.c_cc[VTIME] = 50; // Volta para 5 segundos
        tcsetattr(fd, TCSANOW, &tty);
    }
    return i;
}


// --- 4. Função Emissor ---

void emissor(const char *file_path, const char *port_name, int baud_rate) {
    printf("📡 EMISSOR | Porta: %s | Baud: %d\n", port_name, baud_rate);
    
    struct stat st;
    if (stat(file_path, &st) != 0) {
        fprintf(stderr, "❌ Erro: Arquivo '%s' não encontrado.\n", file_path);
        return;
    }

    // Abre o arquivo para leitura binária
    FILE *file_handler = fopen(file_path, "rb");
    if (!file_handler) {
        perror("Erro ao abrir arquivo para leitura");
        return;
    }

    // Extrai o nome do arquivo (simulação de os.path.basename)
    const char *file_name = strrchr(file_path, '/');
    if (!file_name) file_name = strrchr(file_path, '\\');
    if (!file_name) file_name = file_path;
    else file_name++; // Pula o '/' ou '\'

    size_t file_size = st.st_size;
    int serial_fd = -1;
    
    // Buffer para o pacote: [CRC (4)] + [Tamanho Real (4)] + [Dados (100 bytes preenchidos)]
    unsigned char packet[PACKET_SIZE];
    unsigned char data_block[BLOCK_SIZE];
    char response_buffer[MAX_FILENAME_LEN + 32]; // Buffer para as respostas de sinalização

    // Abertura da porta serial
    serial_fd = setup_serial_port(port_name, baud_rate);
    if (serial_fd < 0) {
        fclose(file_handler);
        return;
    }

    size_t start_offset = 0;
    
    // --- 1. HANDSHAKE DE RETOMADA (Camada de Aplicação) ---

    // Envia a solicitação de status do arquivo
    int request_len = snprintf(response_buffer, sizeof(response_buffer), "%s%s\n", 
                               FILE_STATUS_SIGNAL, file_name);
    write(serial_fd, response_buffer, request_len);
    printf("-> Solicitando STATUS do arquivo '%s'...\n", file_name);
    
    ssize_t n_read = serial_readline(serial_fd, response_buffer, sizeof(response_buffer));
    response_buffer[n_read > 0 ? n_read : 0] = '\0';

    if (n_read > 0 && strncmp(response_buffer, ACK_POS_SIGNAL, strlen(ACK_POS_SIGNAL)) == 0) {
        long resume_offset;
        // Pula o sinalizador e tenta ler o offset
        char *offset_str = response_buffer + strlen(ACK_POS_SIGNAL);
        offset_str = strtok(offset_str, "\n"); // Remove o \n e o que vier depois
        
        if (offset_str && (resume_offset = atol(offset_str)) >= 0) {
            if ((size_t)resume_offset > 0 && (size_t)resume_offset < file_size) {
                start_offset = (size_t)resume_offset;
                fseek(file_handler, start_offset, SEEK_SET); // Pula bytes no arquivo de origem
                printf("-> **RETOMADA SOLICITADA** a partir do byte: %zu (%.2f%%)\n", 
                       start_offset, (double)start_offset / file_size * 100.0);
            } else if ((size_t)resume_offset >= file_size) {
                printf("-> ✅ Arquivo já está COMPLETO no Receptor. Parando.\n");
                goto cleanup;
            } else {
                printf("-> Retomada inválida (Offset 0). Iniciando do zero.\n");
            }
        } else {
            printf("-> Resposta ACK_POS inválida. Iniciando do zero.\n");
        }
    } else if (n_read > 0 && strncmp(response_buffer, START_NEW_SIGNAL, strlen(START_NEW_SIGNAL) - 1) == 0) {
        printf("-> Receptor solicitou início de uma nova transferência.\n");
    } else {
        printf("-> Resposta inesperada ou vazia. Iniciando do zero. ('%s')\n", response_buffer);
    }
        
    // --- 2. LOOP DE TRANSMISSÃO ---
    size_t bytes_sent = start_offset;
    
    // Envia o sinal 'START:' que o Receptor espera para iniciar o loop de bytes.
    request_len = snprintf(response_buffer, sizeof(response_buffer), "%s%s\n", 
                           START_TRANSMISSION_SIGNAL, file_name);
    write(serial_fd, response_buffer, request_len);
    
    printf("\nIniciando transmissão...\n");
    
    while (1) {
        size_t real_size = fread(data_block, 1, BLOCK_SIZE, file_handler);
        
        if (real_size == 0) {
            if (ferror(file_handler)) {
                perror("Erro ao ler o arquivo");
            }
            break; // Fim do arquivo
        }

        // PADDING DO BLOCO: Zera o restante do bloco se real_size < BLOCK_SIZE
        if (real_size < BLOCK_SIZE) {
            memset(data_block + real_size, 0x00, BLOCK_SIZE - real_size);
        }
        
        // Camada de Enlace: Calcula CRC32 e empacota Tamanho Real
        uint32_t checksum = calculate_crc32(data_block, BLOCK_SIZE);
        uint32_t real_size_u32 = (uint32_t)real_size; // Tamanho real em 4 bytes

        // Empacotamento do Pacote: [CRC (4)] + [Tamanho Real (4)] + [Dados (100)]
        // Usamos memcpy para garantir que copiamos exatamente 4 bytes de cada uint32_t,
        // mantendo a ordem de bytes nativa (que deve ser consistente em ambos os lados).
        memcpy(packet, &checksum, sizeof(uint32_t));
        memcpy(packet + CRC_SIZE, &real_size_u32, sizeof(uint32_t));
        memcpy(packet + CRC_SIZE + sizeof(uint32_t), data_block, BLOCK_SIZE);
        
        // Envia o pacote (108 bytes fixos)
        ssize_t n_written = write(serial_fd, packet, PACKET_SIZE);
        if (n_written != PACKET_SIZE) {
            perror("\nErro ao escrever na porta serial (Escrita incompleta/erro)");
            break;
        }

        bytes_sent += real_size;
        
        // Atualização do progresso (simulação de sys.stdout.write)
        printf("\r  Enviando... %zu bytes | Progresso: %.2f%%", 
               bytes_sent, (double)bytes_sent / file_size * 100.0);
        fflush(stdout);
        
        // Pequena pausa para controle de fluxo (0.005s)
        usleep(5000); 
    }

    // 3. Enviar sinalizador de Fim
    write(serial_fd, END_SIGNAL, strlen(END_SIGNAL));
    printf("\n✅ Transmissão concluída. Total de dados enviados: %zu bytes.\n", bytes_sent);

cleanup:
    if (file_handler) fclose(file_handler);
    if (serial_fd >= 0) {
        printf("Porta serial fechada.\n");
        close(serial_fd);
    }
}

// --- 5. Função Receptor ---

void receptor(const char *port_name, int baud_rate) {
    printf("👂 RECEPTOR | Porta: %s | Baud: %d\n", port_name, baud_rate);
    
    int serial_fd = -1;
    FILE *output_file_handler = NULL;
    char current_file_path[MAX_FILENAME_LEN + 10]; // Ex: "recebido_meuarquivo.txt"
    char output_file_name[MAX_FILENAME_LEN];
    int END_SIGNAL_RECEIVED = 0; 
    
    unsigned char packet_buffer[PACKET_SIZE];
    char line_buffer[MAX_FILENAME_LEN + 32];
    
    // Abertura e configuração da porta serial
    serial_fd = setup_serial_port(port_name, baud_rate);
    if (serial_fd < 0) return;

    // 1. Espera o cabeçalho de STATUS
    printf("Aguardando solicitação de STATUS do arquivo...\n");
    char *file_name = NULL;
    
    while (1) {
        ssize_t n_read = serial_readline(serial_fd, line_buffer, sizeof(line_buffer));
        if (n_read < 0) return; // Erro fatal
        if (n_read == 0) {
            printf("Timeout ao aguardar STATUS. Verifique a conexão do Emissor.\n");
            goto cleanup;
        }

        // Tenta encontrar o sinal STATUS:
        if (strncmp(line_buffer, FILE_STATUS_SIGNAL, strlen(FILE_STATUS_SIGNAL)) == 0) {
            // Extrai o nome do arquivo
            file_name = line_buffer + strlen(FILE_STATUS_SIGNAL);
            // Remove o \n e espaços do final
            char *end = strchr(file_name, '\n');
            if (end) *end = '\0';
            
            strncpy(output_file_name, file_name, MAX_FILENAME_LEN);
            output_file_name[MAX_FILENAME_LEN - 1] = '\0';
            break;
        } else {
            printf("  Ignorando dado inesperado antes do STATUS: %s", line_buffer);
        }
    }
    
    // --- 2. LÓGICA DE CHECKPOINTING/RESUME ---
    sprintf(current_file_path, "recebido_%s", output_file_name);
    
    size_t resume_offset = 0;
    char *response_signal = START_NEW_SIGNAL;
    
    struct stat st;
    if (stat(current_file_path, &st) == 0) {
        if (st.st_size > 0) {
            resume_offset = st.st_size;
            // Prepara o sinal ACK_POS: com o offset atual
            snprintf(line_buffer, sizeof(line_buffer), "%s%zu\n", ACK_POS_SIGNAL, resume_offset);
            response_signal = line_buffer;
            printf("-> Arquivo EXISTE. Solicitando retomada do byte: %zu\n", resume_offset);
        } else {
            printf("-> Arquivo existe, mas está vazio. Reiniciando a transferência.\n");
        }
    } else {
        printf("-> Arquivo não encontrado. Iniciando nova transferência.\n");
    }

    // Envia sinal de resposta ao Emissor (ACK_POS: ou START_NEW\n)
    write(serial_fd, response_signal, strlen(response_signal));
    usleep(100000); // 0.1s de pausa

    // Abre o arquivo (append 'a' ou write 'w')
    output_file_handler = fopen(current_file_path, (resume_offset > 0) ? "ab" : "wb");
    if (!output_file_handler) {
        perror("Erro ao abrir arquivo para escrita");
        goto cleanup;
    }
    
    // Valida o sinal START: antes de iniciar o loop de bytes
    printf("-> Aguardando sinal de INÍCIO da Transmissão...\n");
    ssize_t start_ack_read = serial_readline(serial_fd, line_buffer, sizeof(line_buffer));

    if (start_ack_read <= 0 || strncmp(line_buffer, START_TRANSMISSION_SIGNAL, strlen(START_TRANSMISSION_SIGNAL)) != 0) {
        fprintf(stderr, "❌ ERRO GRAVE: Sinal de INÍCIO da Transmissão (START:) não recebido. Linha: %s\n", line_buffer);
        goto cleanup;
    }
    
    size_t bytes_received = resume_offset;
    uint32_t error_count = 0;
    
    // 3. LOOP PRINCIPAL DE RECEPÇÃO DE BLOCOS (108 bytes fixos)
    
    unsigned char data_block[BLOCK_SIZE];
    uint32_t received_checksum;
    uint32_t real_size_u32;
    
    while (1) {
        ssize_t n_read = read(serial_fd, packet_buffer, PACKET_SIZE);
        
        // --- Tratamento de Sinal de FIM/Timeout/Erro ---
        if (n_read == 0) {
            printf("\n  Timeout na leitura de bloco. Transmissão interrompida inesperadamente.\n");
            break;
        }
        if (n_read < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                printf("\n  Timeout na leitura de bloco. Transmissão interrompida inesperadamente.\n");
            } else {
                perror("\nErro de leitura serial");
            }
            break;
        }

        // Checa se o sinal de FIM foi recebido (pode estar no meio do buffer)
        if (n_read < PACKET_SIZE && strstr((char*)packet_buffer, END_SIGNAL) != NULL) {
             END_SIGNAL_RECEIVED = 1;
             printf("\n✅ Recebido sinalizador de FIM.\n");
             break;
        }

        // Erro de sincronização: leu menos que o tamanho esperado do pacote
        if ((size_t)n_read != PACKET_SIZE) {
            fprintf(stderr, "\n  ERRO GRAVE: Falha na leitura do pacote (Esperado %d bytes, leu %zu). Desalinhamento.\n", 
                    PACKET_SIZE, (size_t)n_read);
            break;
        }
        
        // Desempacotamento: [CRC (4)] + [Tamanho Real (4)] + [Dados (100)]
        memcpy(&received_checksum, packet_buffer, CRC_SIZE);
        memcpy(&real_size_u32, packet_buffer + CRC_SIZE, sizeof(uint32_t));
        memcpy(data_block, packet_buffer + CRC_SIZE + sizeof(uint32_t), BLOCK_SIZE);

        size_t real_size = (size_t)real_size_u32;
        
        // Validação da Camada de Enlace
        uint32_t calculated_checksum = calculate_crc32(data_block, BLOCK_SIZE); 

        if (received_checksum == calculated_checksum && real_size <= BLOCK_SIZE) {
            // Escreve APENAS o real_size de bytes válidos no arquivo
            size_t n_written = fwrite(data_block, 1, real_size, output_file_handler);
            if (n_written != real_size) {
                 perror("\nErro ao escrever no arquivo de destino");
                 break;
            }
            bytes_received += real_size;
            
            // Atualização do progresso
            printf("\r  Recebendo... Total: %.2f KB | Erros CRC: %u", 
                   (double)bytes_received / 1024.0, error_count);
            fflush(stdout);

        } else {
            error_count++;
            // Em protocolos seriais simples, a retransmissão não é implementada no enlace.
            // Apenas descarta o bloco e espera o próximo.
        }
    }

    printf("\nRecepção finalizada. Total de bytes válidos: %zu. Erros de CRC detectados: %u.\n", bytes_received, error_count);

cleanup:
    if (output_file_handler) {
        fclose(output_file_handler);
        if (END_SIGNAL_RECEIVED) {
            printf("Arquivo de destino '%s' FINALIZADO e FECHADO.\n", current_file_path);
        } else if (stat(current_file_path, &st) == 0 && st.st_size > 0) {
            printf("Arquivo de destino '%s' FECHADO. Está PARCIALMENTE COMPLETO para retomada.\n", current_file_path);
        } else {
            printf("Arquivo de destino fechado.\n");
        }
    }
    
    if (serial_fd >= 0) {
        close(serial_fd);
        printf("Porta serial fechada.\n");
    }
}

// --- 6. Função Principal ---

int main(int argc, char *argv[]) {
    // Inicializa a tabela CRC uma vez
    generate_crc_table(); 

    if (argc < 4) {
        fprintf(stderr, 
            "Uso: %s <modo> -p <porta> -b <baud> [-f <arquivo>]\n"
            "\n"
            "Argumentos:\n"
            "  <modo>      'emissor' ou 'receptor'\n"
            "  -p <porta>  Porta serial (Ex: /dev/ttyS0, /dev/pts/1)\n"
            "  -b <baud>   Taxa de transmissão (Ex: 9600, 115200)\n"
            "  -f <arquivo> Caminho do arquivo a ser enviado (Obrigatório para emissor)\n", 
            argv[0]);
        return 1;
    }

    char *mode = NULL;
    char *port_name = NULL;
    int baud_rate = 9600;
    char *file_path = NULL;
    
    // Simples parsing de argumentos (poderia usar getopt, mas vamos com o básico)
    mode = argv[1];
    
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "-p") == 0 && (i + 1 < argc)) {
            port_name = argv[i+1];
            i++;
        } else if (strcmp(argv[i], "-b") == 0 && (i + 1 < argc)) {
            baud_rate = atoi(argv[i+1]);
            i++;
        } else if (strcmp(argv[i], "-f") == 0 && (i + 1 < argc)) {
            file_path = argv[i+1];
            i++;
        }
    }

    if (!port_name) {
        fprintf(stderr, "Erro: A porta serial (-p) é obrigatória.\n");
        return 1;
    }
    
    if (strcmp(mode, "emissor") == 0) {
        if (!file_path) {
            fprintf(stderr, "Erro: O modo 'emissor' requer o argumento -f/--file.\n");
            return 1;
        }
        emissor(file_path, port_name, baud_rate);
    } else if (strcmp(mode, "receptor") == 0) {
        receptor(port_name, baud_rate);
    } else {
        fprintf(stderr, "Erro: Modo inválido. Use 'emissor' ou 'receptor'.\n");
        return 1;
    }

    return 0;
}