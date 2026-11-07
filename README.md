# Grok Desk BETA (PAPER)

Versione semplificata del trading desk per Railway.

## Cosa fa
- Ascolta le monete che escono da pump.fun
- Filtra liquidità > $40k e età > 10 min
- Simula PAPER (stack iniziale $200)
- Stop -6% + trailing
- Pagina web nera semplice

**Nessun ordine reale.** Solo simulazione.

## Deploy su Railway (gratis)

1. Vai su [railway.app](https://railway.app) e fai login con GitHub
2. New Project → Deploy from GitHub repo (oppure "Empty Project")
3. Se usi Empty Project:
   - Aggiungi un servizio
   - Collega questo codice (o fai upload della cartella)
4. Railway rileva automaticamente il `Procfile`
5. Clicca Deploy

Quando è online ti dà un URL tipo:
`https://tuo-progetto.up.railway.app`

Apri quell’URL sul telefono.

## Locale

```bash
pip install -r requirements.txt
python main.py
```

Poi apri http://localhost:8000

## Note
- Il piano gratis di Railway dà $5 di credito al mese (di solito bastano)
- Il processo può andare in sleep se non c’è traffico: apri la pagina ogni tanto per tenerlo sveglio
- Per tenerlo sempre acceso serve un piano a pagamento o un free VPS (Oracle Cloud, Fly.io)
