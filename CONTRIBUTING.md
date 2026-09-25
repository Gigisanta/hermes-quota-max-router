# Contribuir

El contrato operativo está en [README.md](README.md) y el [inventario vigente de cuentas, modelos y cuotas](docs/PROVIDER-STATUS.md#cuentas-de-maatwork-ordenadas-por-evidencia-editorial). La ruta de inferencia sólo admite modelos con capacidad gratuita de corte duro, cuota de cuenta y evaluación editorial verificadas. Un catálogo público, una clave guardada o una respuesta simulada no habilitan un modelo.

Preparación local:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make lint type-check test
```

Las pruebas usan `fakeredis` y HTTP simulado. No las ejecutes contra cuentas reales ni producción. Agregá pruebas de fallos, cuotas y privacidad cuando cambies el enrutamiento. Mantené tokens, `var/` y prompts fuera de commits y logs.
