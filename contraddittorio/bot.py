import logging
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = "8963134494:AAFqh4-zfgtSSPRM_Yu3-F6yBHTXgbyqy9Y"

def pulisci_report(output_grezzo: str) -> str:
    righe = output_grezzo.splitlines()
    righe_filtrate = []
    tags_da_escludere = ["[pipeline]", "[analyze]", "[evidence]", "[rerank]", "[pattern]", "(venv)", "jacopodipoggio@"]
    for riga in righe:
        if any(tag in riga for tag in tags_da_escludere):
            continue
        righe_filtrate.append(riga)
    return "\n".join(righe_filtrate).strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🧠 *Voltaire*\n\n"
        "Incolla un post per confrontarlo con la letteratura scientifica.\n\n"
        "_Dubitare è giusto_",
        parse_mode="Markdown"
    )

async def analizza_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    testo_post = update.message.text
    messaggio_attesa = await update.message.reply_text(
        "🔍 *Analisi in corso...*\nStimando i claim e interrogando Europe PMC.",
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
            await messaggio_attesa.edit_text("❌ Errore interno durante l'analisi.")
            return
        report_finale = pulisci_report(output_grezzo)
        if not report_finale:
            await messaggio_attesa.edit_text("❓ Nessun claim verificabile estratto.")
            return
        if len(report_finale) > 3500:
            primo = report_finale[:1800]
            secondo = report_finale[1800:3500]
            terzo = report_finale[3500:]
            await messaggio_attesa.edit_text(primo, parse_mode="Markdown", disable_web_page_preview=True)
            await update.message.reply_text(secondo, parse_mode="Markdown", disable_web_page_preview=True)
            if terzo.strip():
                await update.message.reply_text(terzo, parse_mode="Markdown", disable_web_page_preview=True)
        elif len(report_finale) > 1800:
            primo = report_finale[:1800]
            secondo = report_finale[1800:]
            await messaggio_attesa.edit_text(primo, parse_mode="Markdown", disable_web_page_preview=True)
            await update.message.reply_text(secondo, parse_mode="Markdown", disable_web_page_preview=True)
        else:
            await messaggio_attesa.edit_text(report_finale, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Errore: {e}")
        await messaggio_attesa.edit_text("❌ Errore imprevisto.")

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analizza_post))
    print("🧠 Voltaire avviato e in ascolto...")
    application.run_polling()

if __name__ == '__main__':
    main()
