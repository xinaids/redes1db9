// Arquivo: crc.c (Implementação CRC-32/IEEE 802.3)
#include "crc.h"
#include <stdio.h>
#include <stdint.h> 

// Define o polinômio gerador (CRC-32/IEEE 802.3)
#define POLYNOMIAL 0xEDB88320

// Tabela CRC pré-calculada 
uint32_t crc_table[256];

// Função para gerar a tabela CRC
void generate_crc_table() {
    uint32_t i, j, crc;
    for (i = 0; i < 256; ++i) {
        crc = i;
        for (j = 0; j < 8; ++j) {
            crc = (crc & 1) ? (crc >> 1) ^ POLYNOMIAL : (crc >> 1);
        }
        crc_table[i] = crc;
    }
}

// Função principal para calcular o CRC32
uint32_t calculate_crc32(const unsigned char *data, size_t length) {
    uint32_t crc = 0xFFFFFFFF; // Valor inicial
    size_t i;

    for (i = 0; i < length; ++i) {
        crc = (crc >> 8) ^ crc_table[(crc ^ data[i]) & 0xFF];
    }

    // O padrão CRC-32/IEEE inverte o resultado final
    return crc ^ 0xFFFFFFFF; 
}