# OSINT Lab

Ferramenta pessoal e experimental de pesquisa em fontes abertas, com IA local. Roda no próprio computador. Só fontes abertas e consultas passivas.

## Uso

**Windows:** baixe `OSINTLab.exe` (em *Releases* ou no artefato da ação "Executável Windows") e abra. Os dados ficam em `%USERPROFILE%\.osintlab`.

**Código-fonte:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
osintlab servir      # http://127.0.0.1:8765
pytest -q
```

## Licença

MIT. Dependências e fontes de dados têm licenças e termos próprios.
