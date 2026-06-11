# Latinex Equity Tracker

Dashboard del mercado de acciones de Panama (Latinex / Bolsa Latinoamericana de Valores):
precios, dividendos, estados financieros parseados de los informes trimestrales (PDF),
ratios sectoriales, historico anual, comparables internacionales (Yahoo Finance) y
analisis narrativo generado con Claude.

## Correr local

```
pip install -r requirements.txt
python run.py        # http://localhost:8502
```

Crear un archivo `.env` con:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Deploy en Streamlit Community Cloud

App entrypoint: `dashboard.py`. Secrets requeridos (Settings > Secrets):

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
PUBLIC_MODE = "true"
ADMIN_KEY = "una-clave-secreta"
```

En modo publico los visitantes ven todo (datos y analisis cacheados) pero no pueden
regenerar analisis, editar el watchlist ni limpiar caches; el administrador ingresa
la clave en la barra lateral para habilitar esas acciones.

## Notas

- Los datos vienen de endpoints JSON no documentados de latinexbolsa.com (sin auth)
  y los PDFs de `files.latinexbolsa.com`. Precios con retraso de hasta 5 minutos.
- TRENCO y MHCH presentan informes escaneados (imagen): sus estados financieros no
  se pueden extraer automaticamente.
- Esto no es asesoria de inversion; verifica las cifras contra los PDF fuente.
