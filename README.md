# SteamShotBridge

<p align="center">
  <img src="assets/logo_256.png" width="128" height="128" alt="SteamShotBridge Logo" />
</p>

<p align="center">
  <b>Importador e sincronizador moderno de capturas de tela para a biblioteca da Steam.</b>
</p>

---

## 🌟 Recursos

- **Suporte Total a Jogos Não-Steam**: importe e associe screenshots tiradas fora da Steam aos seus jogos favoritos.
- **Renomeação Abrangente**: altere nomes de jogos de qualquer origem (`exe_map`, jogos personalizados, atalhos do `shortcuts.vdf` ou blocos órfãos) com atualização imediata da interface.
- **Fila de Pendências Não Destrutiva**: se a Steam estiver em execução, as alterações são mantidas em fila e aplicadas de forma segura quando a Steam fechar.
- **Segurança de Dados**: backups automáticos com carimbo de data/hora (`.bak_YYYYMMDD_HHMMSS`) e escritas atômicas para evitar qualquer corrupção de arquivos VDF.
- **Interface Gráfica Fluida**: desenvolvida em PySide6 (Qt) com temas Claro e Escuro, animações suaves e visualização em grade ou lista.
- **Bandeja do Sistema**: roda silenciosamente em segundo plano, monitorando pastas e avisando sobre capturas importadas.

---

## 📦 Instalação

### Opção 1: Instalador Windows (.exe)
Baixe o instalador mais recente na aba de **[Releases](https://github.com/Marcospena12/steam-screenshot-importer/releases)** e execute o assistente de instalação.

### Opção 2: Código Fonte (Desenvolvimento)
Clone o repositório e instale as dependências com Python 3.11+:

```powershell
git clone https://github.com/Marcospena12/steam-screenshot-importer.git
cd steam-screenshot-importer
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Para iniciar a aplicação:
```powershell
python -m steamshotbridge.app
```

---

## 🛠️ Build do Executável

Para compilar o binário autônomo do Windows usando o PyInstaller:

```powershell
pyinstaller SteamShotBridge.spec
```

Os arquivos compilados estarão disponíveis em `dist/SteamShotBridge/`.

Para gerar o instalador (requer Inno Setup 6 instalado):
```powershell
iscc installer.iss
```

O instalador será gerado em `dist-installer/`.

---

## 🧪 Testes

Execute a suíte de testes automatizados com o pytest:

```powershell
pytest
```

---

## 📄 Licença

Este projeto é distribuído sob os termos da licença [MIT](LICENSE).
