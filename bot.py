from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import yt_dlp
import os


TOKEN = "8793223432:AAGRhJ6nx226nRDrr6abgBRPZVHdzgIUTuU"


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = update.message.text

    await update.message.reply_text(
        "⏳ جاري التحميل..."
    )

    try:
        filename = "video.mp4"

        options = {
            "format": "mp4",
            "outtmpl": filename,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])


        await update.message.reply_video(
            video=open(filename, "rb"),
            caption="✅ تم التحميل"
        )

        os.remove(filename)

    except Exception as e:

        await update.message.reply_text(
            f"❌ صار خطأ:\n{e}"
        )


def main():

    app = Application.builder().token(
        TOKEN
    ).build()


    app.add_handler(
        MessageHandler(
            filters.TEXT,
            download_video
        )
    )


    print("بوت التحميل يعمل الآن...")

    app.run_polling()


if __name__ == "__main__":
    main()