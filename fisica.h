// Arquivo: fisica.h
#ifndef FISICA_H
#define FISICA_H

#include <stddef.h>
#include <unistd.h>
#include <stdint.h> 
#include <termios.h>

#define SERIAL_PORT "/dev/ttyS0" 
#define BAUD_RATE B115200         

// Abre e configura a porta serial (Camada Física).
int fisica_inicia(const char *porta_serial);

// Escreve bytes crus na porta serial.
int fisica_escreve(const uint8_t *buffer, size_t len);

// Lê bytes crus da porta serial.
int fisica_le(uint8_t *buffer, size_t len);

// Fecha a porta serial.
void fisica_fecha();

#endif // FISICA_H