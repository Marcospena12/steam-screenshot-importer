# Changelog

Todas as alterações notáveis deste projeto serão documentadas neste arquivo.

O formato é baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/)
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [0.1.0] - 2026-11-04

### Adicionado
- Importação automática de capturas de tela para jogos Steam e jogos não-Steam.
- Resolução e renomeação de títulos de jogos em todas as origens (`exe_map`, `custom_games`, atalhos manuais e órfãos).
- Prioridade determinística de nomes com `name_overrides`.
- Sincronização segura de arquivos VDF da Steam (`shortcuts.vdf` e `screenshots.vdf`) com backups automáticos e escritas atômicas.
- Fila de pendências persistente quando a Steam estiver aberta, permitindo aplicação posterior com um clique ou na inicialização.
- Interface gráfica moderna com suporte a temas Claro/Escuro (PySide6 / Qt).
- Splash screen inicial exibindo o logotipo da aplicação com fechamento suave.
- Ícones em múltiplos tamanhos para bandeja e executável do Windows.
- Suporte a autostart no Windows com compatibilidade para binários congelados (PyInstaller).
- Proteção de segurança contra descompressão de imagens excessivas e limites de download por rede.
- Scripts de empacotamento com PyInstaller e instalador Inno Setup.
