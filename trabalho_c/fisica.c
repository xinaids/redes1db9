// Arquivo: fisica.c
#include "fisica.h"
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <termios.h>
#include <errno.h>
#include <string.h>
#include <stdint.h> 

static int serial_fd = -1;

int fisica_inicia(const char *porta_serial) {
    serial_fd = open(porta_serial, O_RDWR | O_NOCTTY | O_NDELAY);

    if (serial_fd == -1) {
        perror("FISICA: Erro ao abrir a porta serial");
        return -1;
    }

    struct termios tty;

    if (tcgetattr(serial_fd, &tty) != 0) {
        perror("FISICA: Erro ao obter atributos");
        fisica_fecha();
        return -1;
    }

    cfsetospeed(&tty, BAUD_RATE);
    cfsetispeed(&tty, BAUD_RATE);

    tty.c_cflag &= ~PARENB;     // Sem Paridade
    tty.c_cflag &= ~CSTOPB;     // 1 bit de Parada
    tty.c_cflag &= ~CRTSCTS;    // Desabilita Controle de Fluxo por Hardware
    tty.c_cflag |= CS8;         // 8 bits de dados
    tty.c_cflag |= (CLOCAL | CREAD); // Ignora linhas de controle de modem e habilita leitura

    tty.c_iflag &= ~(IXON | IXOFF | IXANY); // Desabilita controle de fluxo por software
    tty.c_iflag &= ~(IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL); // Desabilita processamento especial de input

    tty.c_oflag &= ~OPOST; // Desabilita processamento de output

    tty.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG); // Desabilita modo canônico e eco

    tty.c_cc[VMIN]  = 0;  // Leitura mínima de bytes (0 = não bloqueante)
    tty.c_cc[VTIME] = 5;  // Timeout de 0.5s (5 * 100ms)

    if (tcsetattr(serial_fd, TCSANOW, &tty) != 0) {
        perror("FISICA: Erro ao aplicar atributos");
        fisica_fecha();
        return -1;
    }
    
    // Configura para modo de leitura e escrita padrão (Bloqueante)
    fcntl(serial_fd, F_SETFL, 0); 

    return serial_fd;
}

int fisica_escreve(const uint8_t *buffer, size_t len) {
    if (serial_fd < 0) return -1;
    return write(serial_fd, buffer, len);
}

int fisica_le(uint8_t *buffer, size_t len) {
    if (serial_fd < 0) return -1;
    return read(serial_fd, buffer, len);
}

void fisica_fecha() {
    if (serial_fd >= 0) {
        close(serial_fd);
        serial_fd = -1;
    }
}