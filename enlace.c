// Arquivo: enlace.c
#include "enlace.h"
#include "fisica.h"
#include "crc.h" 

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>

static uint8_t seq_num_emissor = 0;
static uint8_t seq_num_receptor = 0;
static const int MAX_TENTATIVAS = 5;
static const int TIMEOUT_SEGUNDOS = 1; 

// --- Funções Internas ---

static void construir_frame(Frame *frame, uint8_t seq, uint8_t tipo, const uint8_t *dados, uint16_t len) {
    memset(frame, 0, sizeof(Frame));

    frame->flag_inicio = FRAME_FLAG;
    frame->seq_num = seq;
    frame->tipo = tipo;
    frame->len = len;
    
    if (dados && len > 0) {
        memcpy(frame->dados, dados, len);
    }
    
    // Calcula o CRC-32 sobre (Seq + Tipo + Len + Dados)
    uint16_t crc_payload_len = 4 + len;
    uint8_t *crc_buffer = (uint8_t *)&frame->seq_num; 
    
    frame->crc = calculate_crc32(crc_buffer, crc_payload_len);
    
    frame->flag_fim = FRAME_FLAG;
}

static int enviar_frame_fisica(const Frame *frame) {
    size_t total_len = 10 + frame->len; 
    return fisica_escreve((const uint8_t *)frame, total_len);
}

static int esperar_resposta(Frame *resposta_frame) {
    time_t start_time = time(NULL);
    uint8_t *buffer = (uint8_t*)resposta_frame;
    size_t bytes_lidos = 0;

    memset(resposta_frame, 0, sizeof(Frame));

    while (time(NULL) - start_time < TIMEOUT_SEGUNDOS) {
        ssize_t n = fisica_le(buffer + bytes_lidos, sizeof(Frame) - bytes_lidos);

        if (n > 0) {
            bytes_lidos += n;
            // Checagem básica das flags de início e fim.
            if (bytes_lidos > 2 && buffer[0] == FRAME_FLAG && buffer[bytes_lidos - 1] == FRAME_FLAG) {
                if (bytes_lidos >= 10) { // Mínimo para um quadro sem dados (ACK/NAK) com CRC-32
                    return 1; 
                }
            }
        } else if (n < 0 && errno != EWOULDBLOCK && errno != EAGAIN) {
             perror("ENLACE: Erro de leitura serial");
             return -1; 
        }
        usleep(10000); // Espera 10ms antes de tentar ler novamente
    }
    return 0; // Timeout
}

// --- Implementação da Interface de Serviço ---

int enlace_inicia(const char *porta_serial) {
    // 1. Inicializa a Tabela CRC-32 (essencial)
    generate_crc_table(); 

    // 2. Inicializa a Camada Física
    if (fisica_inicia(porta_serial) < 0) {
        return -1;
    }
    
    seq_num_emissor = 0;
    seq_num_receptor = 0;
    printf("ENLACE: Protocolo Stop-and-Wait (CRC-32) iniciado. Port: %s\n", porta_serial);
    return 0;
}

int enlace_enviar_dados(const uint8_t *buffer_dados, size_t len) {
    if (len > MAX_DADOS_PAYLOAD) {
        fprintf(stderr, "ENLACE: Dados muito grandes para um quadro.\n");
        return -1;
    }

    Frame frame_dados;
    Frame frame_resposta;
    int tentativas = 0;

    while (tentativas < MAX_TENTATIVAS) {
        construir_frame(&frame_dados, seq_num_emissor, FRAME_DATA, buffer_dados, (uint16_t)len);
        enviar_frame_fisica(&frame_dados);
        printf("ENLACE: Enviando DATA #%d (T=%d/%d)...\n", seq_num_emissor, tentativas + 1, MAX_TENTATIVAS);

        int status = esperar_resposta(&frame_resposta);

        if (status == 1) { 
            // Recebeu resposta
            if (frame_resposta.tipo == FRAME_ACK && frame_resposta.seq_num == seq_num_emissor) {
                printf("ENLACE: ACK #%d recebido. Avançando sequência.\n", seq_num_emissor);
                seq_num_emissor = 1 - seq_num_emissor; 
                return 0; 
            } 
            printf("ENLACE: Resposta inválida ou duplicada. Retransmitindo...\n");

        } else if (status == 0) { 
            // Timeout
            printf("ENLACE: Timeout. Retransmitindo quadro DATA #%d...\n", seq_num_emissor);
        } else { 
            // Erro fatal
            return -1;
        }

        tentativas++;
    }
    
    fprintf(stderr, "ENLACE: Falha ao enviar quadro DATA após %d tentativas.\n", MAX_TENTATIVAS);
    return -1;
}

static int enviar_resposta_protocolo(uint8_t seq, uint8_t tipo) {
    Frame frame_resp;
    construir_frame(&frame_resp, seq, tipo, NULL, 0); 
    return enviar_frame_fisica(&frame_resp);
}

ssize_t enlace_receber_dados(uint8_t *buffer_dados, size_t max_len) {
    Frame frame_recebido;
    int status;
    uint8_t *crc_buffer;
    uint16_t crc_payload_len;
    uint32_t crc_calculado; 

    while(1) {
        status = esperar_resposta(&frame_recebido); 

        if (status == 0) { continue; } 
        if (status < 0) return -1;

        if (frame_recebido.tipo == FRAME_EOT) {
            return 0; 
        }

        if (frame_recebido.tipo == FRAME_DATA) {
            // 1. Verifica CRC
            crc_payload_len = 4 + frame_recebido.len;
            crc_buffer = (uint8_t *)&frame_recebido.seq_num; 
            
            crc_calculado = calculate_crc32(crc_buffer, crc_payload_len);

            if (crc_calculado != frame_recebido.crc) {
                printf("ENLACE: CRC-32 INCORRETO. Descartando.\n");
                continue; 
            }

            // 2. Verifica Número de Sequência
            if (frame_recebido.seq_num != seq_num_receptor) {
                printf("ENLACE: Quadro duplicado (Seq #%d). Reenviando ACK #%d.\n", frame_recebido.seq_num, 1 - seq_num_receptor);
                enviar_resposta_protocolo(1 - seq_num_receptor, FRAME_ACK);
                continue;
            }

            // 3. Quadro VÁLIDO e NOVO
            printf("ENLACE: Quadro DATA #%d recebido OK. Enviando ACK.\n", seq_num_receptor);
            
            if (frame_recebido.len > max_len) {
                fprintf(stderr, "ENLACE: Buffer da aplicação pequeno. Abortando.\n");
                return -1;
            }
            memcpy(buffer_dados, frame_recebido.dados, frame_recebido.len);
            
            // Envia ACK e avança a sequência
            enviar_resposta_protocolo(seq_num_receptor, FRAME_ACK); 
            seq_num_receptor = 1 - seq_num_receptor;
            
            return frame_recebido.len;
        }
    }
}

int enlace_enviar_eot() {
    Frame frame_eot;
    construir_frame(&frame_eot, 0, FRAME_EOT, NULL, 0); 
    enviar_frame_fisica(&frame_eot);
    printf("ENLACE: Enviando EOT...\n");
    return 0; 
}

void enlace_fecha() {
    fisica_fecha();
    printf("ENLACE: Conexão fechada.\n");
}