// Arquivo: crc.h
#ifndef CRC_H
#define CRC_H

#include <stdint.h>
#include <stddef.h>

// Função para gerar a tabela CRC (DEVE ser chamada em enlace_inicia)
void generate_crc_table(); 

// Função principal para calcular o CRC32
uint32_t calculate_crc32(const unsigned char *data, size_t length);

#endif // CRC_H