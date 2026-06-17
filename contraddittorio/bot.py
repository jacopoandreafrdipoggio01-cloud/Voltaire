import logging
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configurazione Log del Bot
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# METTI QUI IL TUO TOKEN DI TELEGRAM
TOKEN = "8980336613:AAFFd-nhhH0ttFH0C4iGijcUwUJYFvicBI0"

def pulisci_report(output_grezzo: str) -> str:
    """Rimuove i log di debug e tiene solo il report finale."""
    righe = output_grezzo.splitlines()
    righe_filtrate = []
    tags_da_escludere = ["[pipeline]", "[analyze]", "[evidence]", "[rerank]", "[pattern]", "(venv)", "jacopodipoggio@"]
    
    for riga in righe:
        if any(tag in riga for tag in tags_da_escludere):
            continue
        righe_filtrate.append(riga)
        
    return "\n".join(righe_filtrate).strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Messaggio di benvenuto sintetico richiesto."""
    await update.message.reply_text(
        "🧠 *Contraddittorio Bot*\n\n"
        "Incolla un messaggio o inoltra un post qui sotto per confrontarlo direttamente con la letteratura scientifica.",
        parse_mode="Markdown"
    )

async def analizza_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Invia il testo alla CLI e risponde con il Markdown pulito."""
    testo_post = update.message.text
    
    messaggio_attesa = await update.message.reply_text(
        "🔍 *Analisi in corso...*\n"
        "Sto estraendo i claim e interrogando database e revisioni Cochrane. Attendi qualche secondo.",
        parse_mode="Markdown"
    )
    
    try:
        process = subprocess.Popen(
            ['python', '-m', 'contraddittorio.cli'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        output_grezzo, stderr = process.communicate(input=testo_post)
        
        if stderr and not output_grezzo:
            logger.error(f"Errore CLI: {stderr}")
            await messaggio_attesa.edit_text("❌ Si è verificato un errore interno durante l'analisi dei dati.")
            return

        report_finale = pulisci_report(output_grezzo)
        
        if not report_finale:
            await messaggio_attesa.edit_text("❓ Non sono riuscito a estrarre claim verificabili da questo testo.")
            return

        if len(report_finale) > 4000:
           primo = report_finale[:2000]
           secondo = report_finale[2000:]
           await messaggio_attesa.edit_text(primo, parse_mode="Markdown", disable_web_page_preview=True)
           await update.message.reply_text(secondo, parse_mode="Markdown", disable_web_page_preview=True)
        else:
           await messaggio_attesa.edit_text(report_finale, parse_mode="Markdown", disable_web_page_preview=True)
           await messaggio_attesa.edit_text(report_finale, parse_mode="Markdown", disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Errore durante l'elaborazione: {e}")
        await messaggio_attesa.edit_text("❌ Errore imprevisto durante l'analisi del post.")

def main() -> None:
    """Avvia il bot."""
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analizza_post))

    logger.info("Bot avviato e in ascolto...")
    application.run_polling()

if __name__ == '__main__':
    main()
