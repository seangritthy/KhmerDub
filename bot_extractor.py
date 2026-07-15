import os
import asyncio
from telethon import TelegramClient, events

# ==========================================
# CONFIGURATION
# 1. Get your API ID and API HASH from https://my.telegram.org
# 2. Put them here:
API_ID = 0  # Replace with your API ID (integer)
API_HASH = "YOUR_API_HASH"  # Replace with your API HASH (string)
BOT_USERNAME = "@Genz_eBot"
# ==========================================

async def main():
    if API_ID == 0 or API_HASH == "YOUR_API_HASH":
        print("Please edit the script to add your API_ID and API_HASH from https://my.telegram.org")
        input("Press Enter to exit...")
        return

    print("Connecting to Telegram...")
    # 'session_name' will create a file named session_name.session
    # It will prompt for your phone number and login code on the first run.
    client = TelegramClient('session_name', API_ID, API_HASH)
    await client.start()
    print("Connected successfully!")

    print(f"Sending /start to {BOT_USERNAME}...")
    
    # Send a message to the bot
    await client.send_message(BOT_USERNAME, '/start')

    print("Waiting for response... (Listening for 15 seconds)")

    messages_received = []

    # Listen for new messages from the bot
    @client.on(events.NewMessage(chats=BOT_USERNAME))
    async def handler(event):
        text = event.raw_text
        print("\n--- NEW MESSAGE FROM BOT ---")
        print(text)
        
        # Check for buttons (inline keyboard)
        if event.message.buttons:
            print("\n[Buttons attached to this message]:")
            for row in event.message.buttons:
                for button in row:
                    print(f" - {button.text}")
        
        print("----------------------------\n")
        messages_received.append(text)

    # Wait for a bit to let the bot respond
    await asyncio.sleep(15)

    if not messages_received:
        print("No response received from the bot in 15 seconds.")
    else:
        print("Finished extracting initial response.")
        
        # Save to a file
        with open("bot_output.txt", "w", encoding="utf-8") as f:
            for msg in messages_received:
                f.write(msg + "\n\n")
        print("Saved output to bot_output.txt")

    await client.disconnect()
    input("Press Enter to exit...")

if __name__ == '__main__':
    asyncio.run(main())
