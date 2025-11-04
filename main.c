// Arquivo: main.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "aplicacao.h" 

// Declaração de funções (para o compilador)
extern void programa_emissor(const char *caminho_arquivo_entrada);
extern void programa_receptor(const char *caminho_arquivo_saida);

int main(int argc, char *argv[]) {
    if (argc < 2) {
        printf("Modo de Uso:\n");
        printf("Para Emissor: sudo ./protocolo_redes -e <arquivo_para_enviar> \n");
        printf("Para Receptor: sudo ./protocolo_redes -r <arquivo_de_saida> \n");
        return 1;
    }

    if (strcmp(argv[1], "-e") == 0) {
        // MODO EMISSOR
        if (argc < 3) {
            fprintf(stderr, "Erro: Especifique o arquivo a ser enviado.\n");
            return 1;
        }
        programa_emissor(argv[2]);
    } else if (strcmp(argv[1], "-r") == 0) {
        // MODO RECEPTOR
        if (argc < 3) {
            fprintf(stderr, "Erro: Especifique o nome do arquivo de saida.\n");
            return 1;
        }
        programa_receptor(argv[2]);
    } else {
        fprintf(stderr, "Erro: Opcao invalida. Use -e ou -r.\n");
        return 1;
    }

    return 0;
}