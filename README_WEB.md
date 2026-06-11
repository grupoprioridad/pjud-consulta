PJUD Consulta Web

Requisitos:
- Python 3
- pip install -r requirements.txt
- playwright install chromium

Ejecución local:
  PJUD_HEADLESS=0 python3 app.py

Por defecto PJUD_HEADLESS=1.
Nota: el portal PJUD suele bloquear headless en servidores (Imperva). Si la consulta falla, ejecutar en una máquina con navegador real y PJUD_HEADLESS=0.
