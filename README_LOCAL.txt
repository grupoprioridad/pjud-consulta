PJUD Consulta Local

Este paquete está preparado para correr en una laptop con Chrome real.

1) Descomprimir.
2) Abrir terminal dentro de la carpeta.
3) Ejecutar:
   chmod +x start_local.sh
   ./start_local.sh

También puedes hacer doble clic en:
   start_local.command

La app levanta en:
   http://127.0.0.1:8091

Health:
   http://127.0.0.1:8091/health

Si quieres usar Apache en /pjud:
- toma apache-pjud.conf.example
- reemplaza ABSOLUTE_PATH_TO_PROJECT por la ruta real descomprimida
- actívalo en Apache
- deja start_local.sh corriendo

Notas:
- Debes tener Google Chrome instalado.
- No hace falta playwright install porque el sistema usa tu Chrome real.
- El WAF del PJUD debería funcionar mejor en laptop/red residencial que en servidor.
- Por defecto BASE_PATH es /pjud para que calce con Apache.
- Si quieres abrirlo sin Apache, puedes igual usar http://127.0.0.1:8091 directamente.
