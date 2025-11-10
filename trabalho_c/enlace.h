// Arquivo: enlace.h
#ifndef ENLACE_H
#define ENLACE_H

#include <stddef.h>
#include <stdint.h> 
#include <unistd.h>

#define MAX_DADOS_PAYLOAD 1024
// Overhead: 2 Flags + 1 Seq + 1 Tipo + 2 Len + 4 CRC = 10 bytes
#define MAX_FRAME_SIZE (MAX_DADOS_PAYLOAD + 10) 

// Estrutura do Quadro (Frame) do Protocolo de Enlace
typedef struct {
    uint8_t flag_inicio;        // 1 byte: 0x7E
    uint8_t seq_num;            // 1 byte: Número de sequência (0 ou 1)
    uint8_t tipo;               // 1 byte: Tipo do quadro (DATA, ACK, NAK, EOT)
    uint16_t len;               // 2 bytes: Tamanho da carga útil (Payload)
    uint8_t dados[MAX_DADOS_PAYLOAD]; // Dados puros (Payload)
    uint32_t crc;               // 4 bytes: CRC-32
    uint8_t flag_fim;           // 1 byte: 0x7E
} Frame;

// Tipos de Quadros
#define FRAME_FLAG  0x7E
#define FRAME_DATA  0x01
#define FRAME_ACK   0x02
#define FRAME_NAK   0x03
#define FRAME_EOT   0x04 

// Inicializa a Camada Física e Enlace.
int enlace_inicia(const char *porta_serial);

// Primitiva: Envia dados puros da Camada de Aplicação.
int enlace_enviar_dados(const uint8_t *buffer_dados, size_t len);

// Primitiva: Recebe um bloco de dados confiável.
ssize_t enlace_receber_dados(uint8_t *buffer_dados, size_t max_len);

// Envia um quadro de Fim de Transmissão (EOT).
int enlace_enviar_eot();

// Fecha a porta serial.
void enlace_fecha();

#endif // ENLACE_H