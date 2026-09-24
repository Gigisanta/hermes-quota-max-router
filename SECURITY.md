# Seguridad

La versión 1.0 sólo se sirve en `127.0.0.1:8123` y sólo acepta cargas editoriales públicas identificadas mediante un token propio por proyecto. La aplicación rechaza correo electrónico y algunos identificadores privados evidentes como defensa adicional; **el cliente debe clasificar y filtrar los datos antes de enviarlos**. No se debe conectar el chat de Simón ni material personal a este servicio.

Las credenciales de proveedores y los tokens de proyecto viven fuera de Git, en el almacén de secretos de cada proyecto. El catálogo verificado y la cola SQLite también quedan fuera de Git. La cola guarda las solicitudes editoriales públicas pendientes y los resultados terminados durante siete días; el proceso no registra prompts ni valores de claves. Protegé el directorio `var/` y sus copias de respaldo.

El router falla cerrado cuando faltan cuotas verificadas, Redis, precio cero o las cuatro rutas independientes exigidas. Las rutas de evaluación se habilitan únicamente con `ROUTER_PILOT_MODE=1` y no son aptas para publicación. Nunca se usa una API paga ni la GPU local como alternativa.

Reportá vulnerabilidades de forma privada a los responsables de MaatWork; no incluyas tokens ni contenidos de la cola en un reporte público.
