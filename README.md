# Bot de Oportunidades · 3S Superficies

Busca cada noche procesos de contratación pública (SEACE / OSCE-OECE) que
coinciden con las palabras clave de 3S y los deja en el módulo **OPORTUNIDADES**
del sistema para que el comercial los revise.

- **Fuente:** API oficial de datos abiertos OCDS del OECE
  (`https://contratacionesabiertas.oece.gob.pe/api/v1/releasesAfter`). Datos
  estructurados, sin scraping frágil.
- **Qué hace:** baja lo más reciente, filtra por `keywords.txt` (sin distinguir
  acentos ni mayúsculas), y hace *upsert* en Supabase llamando a la función
  `upsert_opportunity`. Idempotente: si un proceso ya existe, refresca sus datos
  pero **no pisa** el estado que puso el comercial (interesado/descartado).
- **Cuándo:** una vez al día, 20:00 hora Perú (cron de Railway).

> Requisito previo: haber corrido la migración `0021_opportunities.sql` en
> Supabase (crea la tabla y la función `upsert_opportunity`).

## Desplegar en Railway

1. **Sube esta carpeta a un repositorio** (uno nuevo, aparte de 3s-inventario) o
   despliega con la CLI de Railway (`railway up`) desde esta carpeta.
2. En Railway → **New Project → Deploy from GitHub repo** (elige el repo).
   Nixpacks detecta Python por `requirements.txt` solo.
3. **Variables** (Railway → Variables):
   - `SUPABASE_URL` = la URL de tu proyecto (Supabase → Project Settings → API).
   - `SUPABASE_SERVICE_ROLE_KEY` = la **service_role** key (misma pantalla).
     Es secreta y permite escribir; va solo aquí, **nunca** en el frontend.
   - `LOOKBACK_DAYS` = `2` (opcional).
4. **Programar el cron:** en `railway.json` ya va `"cronSchedule": "0 1 * * *"`.
   Eso es **01:00 UTC = 20:00 en Perú** (Perú es UTC-5 todo el año, sin horario
   de verano). Railway lo toma al desplegar; también puedes verlo/ajustarlo en
   Settings → Cron Schedule.

## Primera corrida: valida antes de confiar

Como la API del OECE solo se puede probar con acceso real, haz una corrida de
prueba antes de dejarlo automático:

1. Agrega temporalmente la variable `DRY_RUN=1`.
2. Dispara el servicio una vez (Railway → Deployments → Run / o Redeploy).
3. Mira los **logs**. Debe terminar con una línea como:
   `== Fin. páginas:N vistas:X coinciden:Y guardadas:Z ==`
   - Si `vistas` > 0, la API responde bien.
   - Si da error de descarga, revisa `OECE_API_BASE` (la ruta pudo cambiar; la
     alternativa es `recordsAfter` en vez de `releasesAfter`).
4. Si todo se ve bien, **quita `DRY_RUN`** y deja el cron. Al día siguiente a las
   20:00 (o disparándolo tú), las oportunidades aparecen en el módulo.

## Probar en tu PC (opcional)

```
pip install -r requirements.txt
DRY_RUN=1 python scraper.py      # imprime lo que haría, sin escribir
```

## Palabras clave

Edita `keywords.txt` (una por línea). Ya vienen las 15 que definiste. Para
agregar o quitar, edita el archivo y vuelve a desplegar.
