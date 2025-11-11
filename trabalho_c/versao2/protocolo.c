#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>
#include <time.h> 
#include <sys/select.h> // Adicionado para a função select()

// --- 1. Configurações Globais ---
#define BLOCK_SIZE 100 
#define CRC_SIZE 4
// Define o tamanho máximo de um pacote (dados + controle)
#define MAX_PACKET_SIZE (CRC_SIZE + sizeof(uint32_t) + BLOCK_SIZE) 
#define MAX_FILENAME_LEN 256

// --- Sinalizadores de Controle e Checkpointing ---
#define START_TRANSMISSION_SIGNAL "START:" // Sinal de início antes do loop de envio de blocos
#define END_SIGNAL "END\n"

// CORREÇÃO 1: Definir ACK/NAK como char simples
#define ACK_CHAR 'A' // Sinal de ACK (Usaremos variáveis char para enviá-lo)
#define NAK_CHAR 'N' // Sinal de NAK (Usaremos variáveis char para enviá-lo)

// --- Configuração do Protocolo Stop-and-Wait ---
#define MAX_RETRIES 5 // Número máximo de tentativas antes de abortar
#define TIMEOUT_SEC 3 // Tempo de espera para o ACK em segundos

// --- 2. Camada de Enlace: Módulo CRC32 ---
#define POLYNOMIAL 0xEDB88320
uint32_t crc_table[256] = {0};

/**
 * @brief Gera a tabela de CRC32.
 */
void generate_crc_table() {
    // Evita recalcular a tabela se ela já foi inicializada
    if (crc_table[1] != 0) return; 

    uint32_t i, j, crc;
    for (i = 0; i < 256; ++i) {
        crc = i;
        for (j = 0; j < 8; ++j) {
            crc = (crc & 1) ? (crc >> 1) ^ POLYNOMIAL : (crc >> 1);
        }
        crc_table[i] = crc;
    }
}

/**
 * @brief Calcula o CRC32 (IEEE 802.3) de um bloco de dados.
 * @param data Ponteiro para os dados.
 * @param length Tamanho dos dados.
 * @return uint32_t O valor do CRC32.
 */
uint32_t calculate_crc32(const unsigned char *data, size_t length) {
    if (crc_table[1] == 0) {
        generate_crc_table();
    }
    uint32_t crc = 0xFFFFFFFF; // Valor inicial
    size_t i;

    for (i = 0; i < length; ++i) {
        crc = (crc >> 8) ^ crc_table[(crc ^ data[i]) & 0xFF];
    }

    return crc ^ 0xFFFFFFFF; // Valor final
}

// --- 3. Camada Física: Módulo Serial ---

/**
 * @brief Configura a porta serial com as flags RAW (Não Canônico), 8N1 e CRTSCTS.
 * @param portname Nome da porta serial (ex: /dev/pts/1).
 * @param baudrate Taxa de transmissão (ex: 115200).
 * @param min_bytes_to_read Número mínimo de bytes a serem lidos. (Corrigido o nome)
 * @param read_timeout_tenths Tempo limite de leitura (em décimos de segundo). (Corrigido o nome)
 * @return int File descriptor (fd) da porta serial aberta, ou -1 em caso de erro.
 */
int serial_setup(const char *portname, int baudrate, int min_bytes_to_read, int read_timeout_tenths) {
    // O_NDELAY será desativado depois para re-habilitar o bloqueio de VMIN/VTIME.
    int fd = open(portname, O_RDWR | O_NOCTTY | O_NDELAY);
    if (fd == -1) {
        perror("Erro ao abrir a porta serial");
        return -1;
    }

    struct termios tty;
    memset(&tty, 0, sizeof(tty));

    // Obter atributos atuais
    if (tcgetattr(fd, &tty) != 0) {
        perror("Erro ao obter atributos da porta serial");
        close(fd);
        return -1;
    }

    // --- 1. Configurar Taxa de Transmissão (Baud Rate) ---
    speed_t speed;
    switch (baudrate) {
        case 9600: speed = B9600; break;
        case 115200: speed = B115200; break;
        default: speed = B115200; break; // Default seguro
    }

    cfsetospeed(&tty, speed);
    cfsetispeed(&tty, speed);

    // --- 2. Configurações de Controle (c_cflag) ---
    // CLOCAL: Ignora linhas de controle de modem (necessário para socat/virtual)
    // CREAD: Habilita o receptor
    tty.c_cflag |= (CLOCAL | CREAD); 
    tty.c_cflag &= ~CSIZE;
    tty.c_cflag |= CS8;       // 8 bits por byte
    tty.c_cflag &= ~PARENB;   // Sem paridade
    tty.c_cflag &= ~CSTOPB;   // 1 bit de parada

    // *** PONTO CRÍTICO: HABILITAR CONTROLE DE FLUXO POR HARDWARE (RTS/CTS) ***
    // Isso é essencial para evitar estouros de buffer (o lixo que apareceu).
    tty.c_cflag |= CRTSCTS; 

    // --- 3. Configurações de Entrada (c_iflag) ---
    // Desabilita processamento canônico (RAW Mode):
    // IGNBRK, BRKINT, PARMRK, ISTRIP, INLCR, IGNCR, ICRNL: Desliga todas as traduções
    // IXON: Desliga Controle de Fluxo por Software (XON/XOFF)
    tty.c_iflag &= ~(IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL | IXON | IXOFF); 
    
    // --- 4. Configurações de Saída (c_oflag) ---
    // Saída Raw (sem remapeamento)
    tty.c_oflag &= ~OPOST; 
    
    // --- 5. Configurações de Local (c_lflag) ---
    // Desabilita ECHO, canonical mode (ICANON) e processamento de sinais (ISIG)
    tty.c_lflag &= ~(ECHO | ECHONL | ICANON | ISIG); 

    // --- 6. Configurações de Leitura (VMIN/VTIME) ---
    // VMIN: Mínimo de caracteres para read() retornar.
    // VTIME: Tempo limite para read() retornar (em décimos de segundo).
    tty.c_cc[VMIN] = min_bytes_to_read;
    tty.c_cc[VTIME] = read_timeout_tenths; 

    // Limpar buffers de entrada e saída e aplicar configurações imediatamente
    tcflush(fd, TCIFLUSH);
    if (tcsetattr(fd, TCSANOW, &tty) != 0) {
        perror("Erro ao definir atributos da porta serial");
        close(fd);
        return -1;
    }

    // Habilita o modo de bloqueio para respeitar VMIN/VTIME
    fcntl(fd, F_SETFL, 0); 

    // Retornar o file descriptor (fd)
    return fd;
}

/**
 * @brief Lê um certo número de bytes com um timeout específico, usando select().
 * @param fd File descriptor da porta serial.
 * @param buffer Buffer para armazenar os dados lidos.
 * @param size Número de bytes a ler.
 * @param timeout_sec Tempo limite em segundos.
 * @return ssize_t Número de bytes lidos, 0 se timeout, ou -1 em caso de erro.
 */
ssize_t serial_read_with_timeout(int fd, char *buffer, size_t size, int timeout_sec) {
    fd_set set;
    struct timeval timeout;
    int rv;
    ssize_t bytes_read = 0;

    // Configura o set de file descriptors e o timeout
    FD_ZERO(&set);
    FD_SET(fd, &set);
    timeout.tv_sec = timeout_sec;
    timeout.tv_usec = 0;

    // Espera por dados na porta serial
    rv = select(fd + 1, &set, NULL, NULL, &timeout);

    if (rv == -1) {
        perror("Erro em select()");
        return -1;
    } else if (rv == 0) {
        // Timeout
        return 0; 
    } else {
        // Dados disponíveis, tenta ler
        bytes_read = read(fd, buffer, size);
        return bytes_read;
    }
}

/**
 * @brief Escreve um certo número de bytes na porta serial.
 * @param fd File descriptor da porta serial.
 * @param buffer Buffer contendo os dados a serem escritos.
 * @param size Número de bytes a escrever.
 * @return ssize_t Número de bytes escritos, ou -1 em caso de erro.
 */
ssize_t serial_write(int fd, const char *buffer, size_t size) {
    ssize_t written = write(fd, buffer, size);
    if (written < 0) {
        perror("Erro ao escrever na porta serial");
    }
    // Garante que o buffer de escrita seja esvaziado imediatamente
    tcdrain(fd); 
    return written;
}

// --- 4. Camada de Aplicação/Enlace: Funções do Protocolo ---

/**
 * @brief Implementa a lógica do EMISSOR.
 * @param fd File descriptor da porta serial.
 * @param file_path Caminho do arquivo a ser enviado.
 */
void emissor_mode(int fd, const char *file_path) {
    FILE *file = NULL;
    struct stat st;

    // 1. Obter tamanho e nome do arquivo
    if (stat(file_path, &st) == -1) {
        perror("Erro ao obter informações do arquivo");
        return;
    }
    long file_size = st.st_size;

    // 2. Tentar abrir o arquivo
    file = fopen(file_path, "rb");
    if (file == NULL) {
        perror("Erro ao abrir o arquivo para leitura");
        return;
    }

    const char *file_name = strrchr(file_path, '/');
    file_name = (file_name != NULL) ? file_name + 1 : file_path;

    if (strlen(file_name) >= MAX_FILENAME_LEN) {
        fprintf(stderr, "Erro: Nome do arquivo muito longo.\n");
        fclose(file);
        return;
    }

    printf("-> EMISSOR: Iniciando Handshake...\n");
    printf("-> EMISSOR: Arquivo '%s' (%ld bytes) pronto para envio.\n", file_name, file_size);

    // 3. Handshake/Sinal de Início
    // Formato: "START:<nome_arquivo><tamanho_arquivo>\n"
    char start_msg[MAX_FILENAME_LEN + 32];
    snprintf(start_msg, sizeof(start_msg), "%s%s%ld\n", 
             START_TRANSMISSION_SIGNAL, file_name, file_size);
    
    // Envia o sinal de START (Handshake)
    if (serial_write(fd, start_msg, strlen(start_msg)) < 0) {
        fprintf(stderr, "Erro ao enviar sinal de START.\n");
        fclose(file);
        return;
    }
    
    long bytes_sent = 0;
    int retry_count = 0;
    
    // --- Loop Principal de Envio (Stop-and-Wait) ---
    while (bytes_sent < file_size) {
        unsigned char packet[MAX_PACKET_SIZE];
        unsigned char data_buffer[BLOCK_SIZE];
        size_t read_size = 0;
        uint32_t current_block_size = 0;
        uint32_t calculated_crc;
        
        // 4. Ler o bloco de dados
        read_size = fread(data_buffer, 1, BLOCK_SIZE, file);
        current_block_size = (uint32_t)read_size;

        // Se a leitura falhou e não estamos no fim do arquivo, aborta
        if (read_size == 0 && !feof(file)) {
            perror("Erro na leitura do arquivo");
            break;
        }

        // 5. Calcular CRC
        calculated_crc = calculate_crc32(data_buffer, read_size);

        // 6. Construir o pacote (CRC | Tamanho Real | Dados)
        memcpy(packet, &calculated_crc, CRC_SIZE); // 4 bytes CRC
        memcpy(packet + CRC_SIZE, &current_block_size, sizeof(uint32_t)); // 4 bytes Tamanho Real
        memcpy(packet + CRC_SIZE + sizeof(uint32_t), data_buffer, read_size); // Dados

        // --- Loop de Retransmissão (Stop-and-Wait) ---
        retry_count = 0;
        while (retry_count <= MAX_RETRIES) {
            
            size_t total_packet_size = CRC_SIZE + sizeof(uint32_t) + read_size;

            printf("-> EMISSOR: Enviando bloco de %ld bytes (Tentativa %d). Total: %ld\n", 
                   read_size, retry_count + 1, bytes_sent);
            
            // 7. Enviar o pacote
            if (serial_write(fd, (char *)packet, total_packet_size) < 0) {
                fprintf(stderr, "Erro de escrita serial, abortando.\n");
                fclose(file);
                return;
            }

            // 8. Aguardar ACK
            char ack_buffer;
            ssize_t ack_read = serial_read_with_timeout(fd, &ack_buffer, 1, TIMEOUT_SEC);

            printf("-> EMISSOR: Aguardando ACK (Timeout: %d segundos)...\n", TIMEOUT_SEC);

            if (ack_read > 0) {
                if (ack_buffer == ACK_CHAR) {
                    // ACK recebido, avança
                    bytes_sent += read_size;
                    printf("<- EMISSOR: ACK recebido. Avançando. Total enviado: %ld\n", bytes_sent);
                    break; 

                } else if (ack_buffer == NAK_CHAR) {
                    // NAK recebido, retransmite
                    printf("! EMISSOR: NAK recebido. Retransmitindo...\n");
                    retry_count++;
                } else {
                    // Resposta inesperada (pode ser lixo do buffer), retransmite
                    printf("! EMISSOR: Resposta inesperada (0x%02X). Retransmitindo...\n", (unsigned char)ack_buffer);
                    retry_count++;
                }
            } else if (ack_read == 0) {
                // Timeout, retransmite
                printf("! EMISSOR: Timeout! Nenhuma resposta recebida. Retransmitindo...\n");
                retry_count++;
            } else {
                // Erro de leitura
                fprintf(stderr, "Erro de leitura serial, abortando.\n");
                fclose(file);
                return;
            }
        }
        
        // Se o loop de retransmissão falhou (excedeu MAX_RETRIES)
        if (retry_count > MAX_RETRIES) {
            fprintf(stderr, "!!! EMISSOR: Excedido o número máximo de retransmissões. Abortando a transferência.\n");
            fclose(file);
            return;
        }

        // Se estamos no fim do arquivo, saímos do loop principal
        if (feof(file)) {
            break;
        }

        // Volta a posição de leitura no arquivo se não enviou o bloco
        if (bytes_sent % BLOCK_SIZE != 0) {
             fseek(file, -read_size, SEEK_CUR); // Ajusta a posição para reler o bloco
        }
    } // Fim do Loop Principal

    // 9. Sinal de Fim
    if (bytes_sent == file_size) {
        printf("-> EMISSOR: Arquivo enviado com sucesso. Enviando sinal de FIM.\n");
        serial_write(fd, END_SIGNAL, strlen(END_SIGNAL));
    } else {
        fprintf(stderr, "!!! EMISSOR: Transferência incompleta (esperado %ld, enviado %ld).\n", file_size, bytes_sent);
    }
    
    fclose(file);
}


/**
 * @brief Implementa a lógica do RECEPTOR.
 * @param fd File descriptor da porta serial.
 */
void receptor_mode(int fd) {
    printf("<- RECEPTOR: Aguardando sinal de START...\n");

    char start_buffer[MAX_FILENAME_LEN + 32];
    ssize_t start_len = 0;
    
    // 1. Aguardar e ler o sinal de START
    // Leitura bloqueante (VMIN/VTIME configurado no serial_setup)
    
    // Usamos um loop para ler até encontrar a quebra de linha do START
    size_t i = 0;
    while(i < sizeof(start_buffer) - 1) {
        ssize_t current_read = read(fd, start_buffer + i, 1);
        if (current_read > 0) {
            if (start_buffer[i] == '\n') {
                start_buffer[i + 1] = '\0';
                start_len = i + 1;
                break;
            }
            i++;
        } else if (current_read == 0) {
            // Timeout ou EOF (Não deve ocorrer com VMIN/VTIME)
            break;
        } else {
            // Erro
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                perror("Erro de leitura do sinal START");
                return;
            }
            // Se for EAGAIN, continuamos
        }
    }

    if (start_len == 0) {
        fprintf(stderr, "Erro: Não foi possível receber o sinal START ou timeout.\n");
        return;
    }
    
    // 2. Parsar o sinal de START
    char file_name[MAX_FILENAME_LEN] = {0};
    long file_size = 0;
    char *start_ptr = start_buffer;

    // Verificar se a string começa com START:
    if (strncmp(start_ptr, START_TRANSMISSION_SIGNAL, strlen(START_TRANSMISSION_SIGNAL)) != 0) {
        fprintf(stderr, "Erro: Sinal de START inválido. Recebido: %s\n", start_buffer);
        return;
    }
    start_ptr += strlen(START_TRANSMISSION_SIGNAL);
    
    // Encontrar o final do nome do arquivo (onde o tamanho começa)
    char *size_ptr = NULL;
    for (char *p = start_ptr; *p != '\0' && *p != '\n'; p++) {
        if (*p >= '0' && *p <= '9') {
            size_ptr = p;
            break;
        }
    }

    if (size_ptr == NULL) {
        fprintf(stderr, "Erro: Sinal de START não contém tamanho do arquivo.\n");
        return;
    }

    // Copiar o nome do arquivo e adicionar terminador nulo
    size_t name_len = size_ptr - start_ptr;
    if (name_len >= MAX_FILENAME_LEN) name_len = MAX_FILENAME_LEN - 1;
    strncpy(file_name, start_ptr, name_len);
    file_name[name_len] = '\0';
    
    // Ler o tamanho do arquivo
    file_size = atol(size_ptr);

    printf("START:%s%ld\n", file_name, file_size);
    printf("<- RECEPTOR: Recebendo arquivo '%s' de %ld bytes.\n", file_name, file_size);

    // 3. Abrir arquivo de destino para escrita
    FILE *dest_file = fopen(file_name, "wb");
    if (dest_file == NULL) {
        perror("Erro ao abrir arquivo de destino para escrita");
        return;
    }

    // 4. Loop Principal de Recebimento (Stop-and-Wait)
    long bytes_received = 0;
    while (bytes_received < file_size) {
        unsigned char packet[MAX_PACKET_SIZE];
        unsigned char data_buffer[BLOCK_SIZE];
        uint32_t received_crc;
        uint32_t current_block_size;
        ssize_t read_count = 0;
        
        size_t expected_block_len = (file_size - bytes_received) < BLOCK_SIZE ? 
                                     (file_size - bytes_received) : BLOCK_SIZE;

        // O pacote terá CRC (4) + Tamanho Real (4) + Dados (max BLOCK_SIZE)
        // size_t expected_packet_len = CRC_SIZE + sizeof(uint32_t) + expected_block_len;
        
        // Tentativa de ler o tamanho máximo do pacote
        read_count = serial_read_with_timeout(fd, (char *)packet, MAX_PACKET_SIZE, TIMEOUT_SEC + 1);

        if (read_count <= 0) {
            if (read_count == 0) {
                fprintf(stderr, "!!! RECEPTOR: Timeout ao aguardar bloco de dados. Abortando.\n");
            } else {
                perror("Erro de leitura serial durante o recebimento do bloco");
            }
            break; 
        }

        // 5. Desempacotar (Assumindo que o cabeçalho CRC + Tamanho vieram)
        if (read_count < CRC_SIZE + sizeof(uint32_t)) {
             fprintf(stderr, "!!! RECEPTOR: Pacote muito curto (%zd bytes). NAK/Rejeitando.\n", read_count);
             // CRTSCTS garantirá que o NAK não se perca
             char nak = NAK_CHAR;
             serial_write(fd, &nak, 1);
             continue;
        }

        memcpy(&received_crc, packet, CRC_SIZE);
        memcpy(&current_block_size, packet + CRC_SIZE, sizeof(uint32_t));
        
        // Verificar se o tamanho real do bloco é válido
        if (current_block_size > BLOCK_SIZE || (CRC_SIZE + sizeof(uint32_t) + current_block_size) != read_count) {
             fprintf(stderr, "!!! RECEPTOR: Tamanho do bloco inconsistente (Tamanho=%u, Lido=%zd). NAK/Rejeitando.\n", current_block_size, read_count);
             char nak = NAK_CHAR;
             serial_write(fd, &nak, 1);
             continue;
        }

        // Copiar os dados para o buffer de verificação
        memcpy(data_buffer, packet + CRC_SIZE + sizeof(uint32_t), current_block_size);

        // 6. Calcular e Verificar CRC
        uint32_t calculated_crc = calculate_crc32(data_buffer, current_block_size);
        
        printf("<- RECEPTOR: Bloco recebido (%u bytes). CRC: Rx=0x%08X, Calc=0x%08X\n", 
               current_block_size, received_crc, calculated_crc);

        if (received_crc == calculated_crc) {
            // CRC OK: Escreve no arquivo e envia ACK
            size_t written = fwrite(data_buffer, 1, current_block_size, dest_file);
            if (written != current_block_size) {
                perror("Erro na escrita do arquivo de destino");
                // Mesmo com erro de escrita, enviamos ACK para avançar (Problema de disco, não de enlace)
            }

            bytes_received += current_block_size;
            char ack = ACK_CHAR;
            serial_write(fd, &ack, 1);
            printf("<- RECEPTOR: ACK enviado. Total recebido: %ld\n", bytes_received);
            
            // Se o bloco recebido for o último, paramos
            if (current_block_size < BLOCK_SIZE) break;

        } else {
            // CRC NOK: Envia NAK e aguarda retransmissão
            fprintf(stderr, "!!! RECEPTOR: Erro de CRC! Enviando NAK.\n");
            char nak = NAK_CHAR;
            serial_write(fd, &nak, 1);
        }
    } // Fim do Loop Principal

    // 7. Aguardar sinal de FIM
    char end_buffer[8] = {0};
    ssize_t end_read = read(fd, end_buffer, 4); // Leitura bloqueante, espera 4 bytes
    
    if (end_read > 0 && strncmp(end_buffer, "END\n", 4) == 0) {
        printf("<- RECEPTOR: Sinal de FIM recebido.\n");
    } else {
         printf("<- RECEPTOR: Não foi recebido o sinal de FIM (ou pacote FINAL foi o último bloco).\n");
    }

    if (bytes_received == file_size) {
        printf("<- RECEPTOR: Transferência de arquivo '%s' concluída com sucesso (%ld bytes).\n", file_name, bytes_received);
    } else {
        fprintf(stderr, "!!! RECEPTOR: Transferência incompleta (esperado %ld, recebido %ld).\n", file_size, bytes_received);
    }

    fclose(dest_file);
}


// --- 5. Função Principal ---

int main(int argc, char *argv[]) {
    // Verificar número mínimo de argumentos
    if (argc < 4) {
        fprintf(stderr, 
            "Uso: %s <modo> -p <porta> -b <baud> [-f <arquivo>]\n"
            "Modos: emissor | receptor\n"
            "Exemplo Emissor: %s emissor -p /dev/pts/2 -b 115200 -f meu_arquivo.bin\n"
            "Exemplo Receptor: %s receptor -p /dev/pts/1 -b 115200\n\n"
            "  -p <porta>  Caminho da porta serial (Ex: /dev/ttyS0, /dev/pts/1)\n"
            "  -b <baud>   Taxa de transmissão (Ex: 9600, 115200 - Padrão: 115200)\n"
            "  -f <arquivo> Caminho do arquivo a ser enviado (Obrigatório para emissor)\n",
            argv[0], argv[0], argv[0]);
        return 1;
    }

    char *mode = NULL;
    char *port_name = NULL;
    int baud_rate = 115200; // Padrão: 115200
    char *file_path = NULL;
    
    // Simples parsing de argumentos
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

    // Inicializa CRC
    generate_crc_table();

    int fd = -1;

    if (strcmp(mode, "emissor") == 0) {
        if (!file_path) {
            fprintf(stderr, "Erro: O caminho do arquivo (-f) é obrigatório para o emissor.\n");
            return 1;
        }
        // Configura o Emissor: min_bytes_to_read=1, read_timeout_tenths=30 (3 segundos)
        fd = serial_setup(port_name, baud_rate, 1, TIMEOUT_SEC * 10); 
        if (fd != -1) {
             printf("Porta serial configurada: %s @ %d baud.\n", port_name, baud_rate);
             emissor_mode(fd, file_path);
        }
    } else if (strcmp(mode, "receptor") == 0) {
        // Configura o Receptor: min_bytes_to_read=1, read_timeout_tenths=0 (Leitura não-bloqueante/select)
        fd = serial_setup(port_name, baud_rate, 1, 0); 
        if (fd != -1) {
            printf("Porta serial configurada: %s @ %d baud.\n", port_name, baud_rate);
            receptor_mode(fd);
        }
    } else {
        fprintf(stderr, "Erro: Modo inválido. Use 'emissor' ou 'receptor'.\n");
        return 1;
    }

    if (fd != -1) {
        close(fd);
    }
    
    return 0;
}