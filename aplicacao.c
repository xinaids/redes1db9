// Arquivo: aplicacao.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h> 
#include "enlace.h"
#include "fisica.h"

#define TAMANHO_BUFFER_APLICACAO MAX_DADOS_PAYLOAD

// --- Emissor: Envia um Arquivo ---
void programa_emissor(const char *caminho_arquivo_entrada) {
    FILE *fp = fopen(caminho_arquivo_entrada, "rb");
    if (!fp) {
        perror("APLICACAO EMISSORA: Erro ao abrir arquivo para envio");
        return;
    }

    unsigned char buffer_dados[TAMANHO_BUFFER_APLICACAO];
    size_t bytes_lidos;

    if (enlace_inicia(SERIAL_PORT) < 0) {
        fclose(fp);
        return;
    }
    
    printf("APLICACAO EMISSORA: Iniciando transferencia do arquivo: %s\n", caminho_arquivo_entrada);

    while ((bytes_lidos = fread(buffer_dados, 1, TAMANHO_BUFFER_APLICACAO, fp)) > 0) {
        if (enlace_enviar_dados(buffer_dados, bytes_lidos) < 0) {
            fprintf(stderr, "APLICACAO EMISSORA: Falha grave de transferencia. Abortando.\n");
            break;
        }
        printf("."); 
        fflush(stdout);
    }

    enlace_enviar_eot();
    
    enlace_fecha();
    fclose(fp);
    printf("\nAPLICACAO EMISSORA: Transferencia concluida (ou abortada).\n");
}


// --- Receptor: Recebe e Salva um Arquivo ---
void programa_receptor(const char *caminho_arquivo_saida) {
    FILE *fp = fopen(caminho_arquivo_saida, "wb");
    if (!fp) {
        perror("APLICACAO RECEPTORA: Erro ao criar arquivo de saida");
        return;
    }

    unsigned char buffer_dados[TAMANHO_BUFFER_APLICACAO];
    ssize_t bytes_recebidos;

    if (enlace_inicia(SERIAL_PORT) < 0) {
        fclose(fp);
        return;
    }
    
    printf("APLICACAO RECEPTORA: Aguardando inicio da transferencia...\n");

    while(1) {
        bytes_recebidos = enlace_receber_dados(buffer_dados, TAMANHO_BUFFER_APLICACAO);

        if (bytes_recebidos > 0) {
            // Escreve os dados recebidos no arquivo de saída
            fwrite(buffer_dados, 1, bytes_recebidos, fp);
            printf("#"); 
            fflush(stdout);
        } else if (bytes_recebidos == 0) {
            printf("\nAPLICACAO RECEPTORA: Fim de transmissao (EOT).\n");
            break;
        } else {
            fprintf(stderr, "APLICACAO RECEPTORA: Erro na Camada de Enlace. Abortando.\n");
            break;
        }
    }

    enlace_fecha();
    fclose(fp);
    printf("APLICACAO RECEPTORA: Arquivo salvo como %s.\n", caminho_arquivo_saida);
}