# Telegram DM Bot

A Telegram bot that intelligently saves bookmarks to [Raindrop.io](https://raindrop.io) with AI-powered metadata extraction. Send any link via Telegram, and the bot will automatically extract the title, summary, relevant tags, and file it in the appropriate folder.

## Features

- 🔗 **Multi-Source Support** – Automatically handles YouTube, Reddit, Instagram, Twitter, blogs, and general web pages
- 🤖 **AI-Powered Metadata** – Uses Google Gemini Flash to generate clean titles, summaries, and tags
- 📺 **YouTube Integration** – Extracts video titles, channel names, and transcript snippets
- 📰 **Reddit Support** – Fetches post titles and body text
- 📁 **Smart Folder Routing** – Gemini intelligently selects the best Raindrop collection for each bookmark
- ⚡ **Async Processing** – Background task handling for fast Telegram responses

## Prerequisites

- Python 3.8+
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Google Gemini API Key (from [Google AI Studio](https://aistudio.google.com/app/apikey))
- Raindrop.io API Token (from [Raindrop settings](https://app.raindrop.io/settings/account))

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Tyler-Bertholdt/telegram-dm-bot.git
   cd telegram-dm-bot
