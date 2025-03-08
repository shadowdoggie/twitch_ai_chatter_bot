import threading
import asyncio
import time
import os
import requests
import websocket
from flask import Flask, request, render_template_string, redirect, url_for, session, flash, jsonify
from twitchio.ext import commands
import re
import hashlib
import secrets
import uuid
import functools
import json
import random
import urllib.parse

# ---------------------
# Configuration Section
# ---------------------

# Twitch credentials and channel details
TWITCH_USERNAME = ""
TWITCH_OAUTH_TOKEN = ""
STREAMER_CHANNEL = ""

# Twitch OAuth Configuration (for web authentication)
TWITCH_CLIENT_ID = ""  # Replace with your Twitch application client ID
TWITCH_CLIENT_SECRET = ""  # Replace with your Twitch application client secret 
TWITCH_REDIRECT_URI = "https://..nl/auth/callback"  # Must match what's registered in your Twitch app
TWITCH_SCOPES = "user:read:email"  # Required scopes for authentication

# Bot owner configuration
BOT_OWNERS = [""]  # Users with full access

# LM Studio API configuration
LM_STUDIO_API_URL = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_MODEL = "gemma-2-9b-it"
DEFAULT_TEMPERATURE = 0.6
DEFAULT_MAX_TOKENS = 100
DEFAULT_NUM_CTX = 4096

# Files for persistent data
SYSTEM_PROMPT_FILE = "system_prompt.txt"
DEFAULT_SYSTEM_PROMPT_FILE = "default_system_prompt.txt"  # File containing the default system prompt
WHITELIST_FILE = "whitelist.txt"
MODERATORS_FILE = "moderators.txt"  # New file to store moderators

# In-memory conversation contexts
user_conversations = {}  # Dictionary to store individual user conversations
global_conversation = []  # Shared context used when tagged with (global)

# Security configuration
SECRET_KEY = secrets.token_hex(32)  # Generate a secure random key for session encryption
# Securely store password hashes with salt
PASSWORD_HASHES = {
    "password1": hashlib.sha256("".encode() + b"salt1").hexdigest(),
    "password2": hashlib.sha256("N0FXqceycJgZZIL".encode() + b"salt2").hexdigest()
}
SESSION_TIMEOUT = 30 * 60  # 30 minutes

# Load system prompt from file or use default
if os.path.exists(SYSTEM_PROMPT_FILE):
    with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
        system_prompt = f.read() or "You are a helpful assistant."
else:
    system_prompt = "You are a helpful assistant."

# Create default system prompt file if it doesn't exist
if not os.path.exists(DEFAULT_SYSTEM_PROMPT_FILE):
    try:
        print(f"Creating default system prompt file at {DEFAULT_SYSTEM_PROMPT_FILE}")
        with open(DEFAULT_SYSTEM_PROMPT_FILE, "w", encoding="utf-8") as f:
            # Save the current system prompt as the default if it exists
            if os.path.exists(SYSTEM_PROMPT_FILE):
                with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as sf:
                    default_content = sf.read() or "You are a helpful assistant."
                f.write(default_content)
            else:
                f.write("You are a helpful assistant.")
        print("Default system prompt file created successfully")
    except Exception as e:
        print(f"Error creating default system prompt file: {e}")

# Hardcoded safety rule that cannot be edited from the portal
SAFETY_RULE = "No racism, sexism, or any discriminatory garbage. "

# Combine the safety rule with the user-editable part of the prompt
system_prompt = SAFETY_RULE + system_prompt

# Global variables
whitelist = set()
should_restart = False  # Flag to indicate if the bot should restart

# Functions for persistent whitelist
def load_whitelist():
    global whitelist
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        whitelist = set(line.strip().lower() for line in lines if line.strip())
    return whitelist

def save_whitelist():
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        for user in sorted(whitelist):
            f.write(f"{user}\n")
    print(f"Whitelist saved with {len(whitelist)} users.")

# Load the whitelist at startup
load_whitelist()

# Global whitelist for allowed Twitch users (stored as lower-case)
whitelist = load_whitelist()

# Functions for moderator management
def load_moderators():
    moderators = []
    if os.path.exists(MODERATORS_FILE):
        with open(MODERATORS_FILE, "r", encoding="utf-8") as f:
            moderators = [line.strip().lower() for line in f if line.strip()]
    return moderators

def save_moderators(moderators):
    with open(MODERATORS_FILE, "w", encoding="utf-8") as f:
        for user in moderators:
            f.write(f"{user}\n")
    print(f"Moderators list saved with {len(moderators)} users.")

# Load moderators from file on startup
moderators = load_moderators()

# Create an empty set for active sessions, to track who's logged in
active_sessions = {}

# ---------------------------
# Twitch IRC WebSocket for sending messages
# ---------------------------
twitch_ws_app = None
ws_lock = threading.Lock()
last_activity = time.time()
reconnect_attempts = 0

def send_message(ws_app, message):
    global last_activity
    with ws_lock:
        if ws_app and ws_app.sock and ws_app.sock.connected:
            try:
                ws_app.send(f"PRIVMSG #{STREAMER_CHANNEL} :{message}")
                last_activity = time.time()
                return True
            except Exception as e:
                print(f"Error sending message: {str(e)}")
                reconnect_websocket()
                return False
        else:
            print("WebSocket not connected, attempting reconnect...")
            reconnect_websocket()
            return False

def on_open(ws):
    global twitch_ws_app, reconnect_attempts
    with ws_lock:
        twitch_ws_app = ws
    token = TWITCH_OAUTH_TOKEN if TWITCH_OAUTH_TOKEN.startswith("oauth:") else f"oauth:{TWITCH_OAUTH_TOKEN}"
    ws.send(f"PASS {token}")
    ws.send(f"NICK {TWITCH_USERNAME}")
    ws.send(f"JOIN #{STREAMER_CHANNEL}")
    reconnect_attempts = 0
    print("[Twitch] WebSocket connected successfully")
    threading.Thread(target=keep_alive, daemon=True).start()

def on_error(ws, error):
    print(f"WebSocket error: {str(error)}")

def on_close(ws, close_status_code, close_msg):
    print(f"WebSocket closed (code: {close_status_code}, message: {close_msg})")
    reconnect_websocket()

def reconnect_websocket():
    global reconnect_attempts
    with ws_lock:
        global twitch_ws_app
        if twitch_ws_app:
            try:
                twitch_ws_app.close()
            except:
                pass
        reconnect_attempts += 1
        delay = min(5 + (2 ** reconnect_attempts), 30)
        print(f"Attempting reconnect in {delay} seconds...")
        time.sleep(delay)
        connect_to_twitch_ws()

def keep_alive():
    while True:
        time.sleep(60)
        with ws_lock:
            if twitch_ws_app and twitch_ws_app.sock and twitch_ws_app.sock.connected:
                try:
                    if time.time() - last_activity > 240:
                        twitch_ws_app.send("PING :tmi.twitch.tv")
                        print("Sent keep-alive PING")
                except:
                    reconnect_websocket()

def connect_to_twitch_ws():
    global twitch_ws_app
    ws = websocket.WebSocketApp(
        "wss://irc-ws.chat.twitch.tv:443",
        on_open=on_open,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()

# ---------------------------
# Twitch Chat Bot using TwitchIO
# ---------------------------
class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=f"oauth:{TWITCH_OAUTH_TOKEN}",
            prefix="!",
            initial_channels=[STREAMER_CHANNEL]
        )
        
    async def event_ready(self):
        print(f"Logged in as | {self.nick}")
        await self.join_channels([STREAMER_CHANNEL])
        await asyncio.sleep(1)
        print("TwitchIO connected channels:")
        for channel in self.connected_channels:
            print(f" - {channel.name}")
        
    async def event_message(self, message):
        global user_conversations, global_conversation
        
        channel_name = getattr(message.channel, 'name', 'N/A')
        author_name = message.author.name if message.author else "None"
        print(f"Received message from {author_name} in channel: {channel_name}")
        
        if not message.author or message.author.name.lower() == TWITCH_USERNAME.lower():
            return
        
        if TWITCH_USERNAME.lower() in message.content.lower():
            if message.author.name.lower() not in whitelist:
                print(f"User {message.author.name} not whitelisted. Ignoring.")
                return
            
            # Check if the message includes the (global) tag
            is_global = "(global)" in message.content.lower()
            
            pattern = r'(?i)@?\b{}\b'.format(re.escape(TWITCH_USERNAME))
            # Remove the bot's name and the (global) tag if present
            user_prompt = re.sub(pattern, '', message.content).strip()
            user_prompt = re.sub(r'\(global\)', '', user_prompt, flags=re.IGNORECASE).strip() or "Hello"
            
            # Choose the appropriate context
            if is_global:
                print(f"Using global context for user {author_name}")
                message_history = global_conversation
            else:
                # Get or initialize user's conversation context
                user_id = message.author.name.lower()
                if user_id not in user_conversations:
                    user_conversations[user_id] = []
                
                message_history = user_conversations[user_id]
                print(f"Using individual context for user {author_name}")
            
            ai_response = await asyncio.to_thread(
                call_lm_studio,
                prompt=user_prompt,
                temperature=DEFAULT_TEMPERATURE,
                max_tokens=DEFAULT_MAX_TOKENS,
                system_prompt=system_prompt,
                model=LM_STUDIO_MODEL,
                num_ctx=DEFAULT_NUM_CTX,
                message_history=message_history
            )
            
            # Update the appropriate conversation history with this exchange
            # First add the user message if not already in history
            if not message_history or message_history[-1]["role"] != "user" or message_history[-1]["content"] != user_prompt:
                message_history.append({"role": "user", "content": user_prompt})
            
            # Then add the assistant's response
            message_history.append({"role": "assistant", "content": ai_response})
            
            # Prevent context from growing too large - based on rough character count estimate
            # A simple heuristic: if total characters > 75% of context window, remove oldest messages
            total_chars = sum(len(msg["content"]) for msg in message_history)
            char_limit = DEFAULT_NUM_CTX * 3  # Rough estimate: 3 chars per token
            
            while total_chars > char_limit * 0.75 and len(message_history) > 2:
                # Remove oldest message pair (user and assistant)
                if len(message_history) >= 2:
                    removed1 = message_history.pop(0)
                    total_chars -= len(removed1["content"])
                    
                    if message_history:  # Check if there are still messages
                        removed2 = message_history.pop(0)
                        total_chars -= len(removed2["content"])
            
            # Save the updated history to the appropriate context
            if is_global:
                global_conversation = message_history
                print(f"Global context updated, now has {len(global_conversation)} messages")
            else:
                user_conversations[user_id] = message_history
                print(f"User {user_id} context updated, now has {len(message_history)} messages")
            
            reply = f"@{message.author.name} {ai_response}"
            await self.handle_reply(reply)
        
        await self.handle_commands(message)

    async def handle_reply(self, reply):
        max_retries = 3
        for attempt in range(max_retries):
            success = send_message(twitch_ws_app, reply)
            if success:
                print(f"Sent reply via WebSocket: {reply}")
                return
            print(f"Send failed (attempt {attempt+1}/{max_retries})")
            await asyncio.sleep(2 ** attempt)
        print(f"Failed to send message after {max_retries} attempts")

def call_lm_studio(prompt, temperature, max_tokens, system_prompt, model, num_ctx, message_history=None):
    # Start with the system message
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add conversation history if available
    if message_history and len(message_history) > 0:
        messages.extend(message_history)
    
    # Add the current user message if not already included in history
    if not message_history or message_history[-1]["role"] != "user" or message_history[-1]["content"] != prompt:
        messages.append({"role": "user", "content": prompt})
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False
    }
    
    try:
        print(f"Sending request to LM Studio API with {len(messages)} messages in context")
        response = requests.post(LM_STUDIO_API_URL, json=payload, timeout=30)
        print(f"Received response with status code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            # Extract the message content from the LM Studio response format
            if "choices" in data and len(data["choices"]) > 0:
                content = data["choices"][0].get("message", {}).get("content", "")
                if content:
                    return content
                else:
                    return "Sorry, I received an empty response from the AI."
            else:
                return "Sorry, I couldn't understand the AI's response format."
        else:
            error_msg = f"Error: Failed to contact LM Studio API. Status code: {response.status_code}"
            print(error_msg)
            if response.content:
                print(f"Response content: {response.content.decode('utf-8', errors='replace')}")
            return error_msg
    except requests.RequestException as e:
        error_msg = f"Network error when contacting LM Studio API: {str(e)}"
        print(error_msg)
        return error_msg
    except json.JSONDecodeError as e:
        error_msg = f"Failed to parse LM Studio response as JSON: {str(e)}"
        print(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Unexpected error when calling LM Studio API: {str(e)}"
        print(error_msg)
        return error_msg

# ---------------------------
# Flask Web Application for Whitelist Management
# ---------------------------
app = Flask(__name__, static_url_path='', static_folder='static')
app.config['SECRET_KEY'] = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = SESSION_TIMEOUT

# HTML Templates with improved styling
TWITCH_LOGIN_TEMPLATE = """
<!doctype html>
<html>
    <head>
        <title>mr_adam420 AI Control Panel - Login</title>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #121212;
                color: #e0e0e0;
                margin: 0;
                padding: 0;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                position: relative;
                overflow: hidden; /* Prevent scrolling */
            }
            
            /* Emote styling */
            .emote {
                position: absolute;
                opacity: 0.5;
                z-index: 0;
                animation: float 20s infinite linear;
                transition: transform 0.3s ease-out;
                cursor: grab;
                /* Ensure emotes stay within viewport */
                max-width: 100%;
                max-height: 100%;
            }
            
            .emote.dragging {
                cursor: grabbing;
                z-index: 10;
                opacity: 0.8;
            }
            
            @keyframes float {
                0% {
                    transform: translateY(0) rotate(0deg);
                }
                50% {
                    transform: translateY(-20px) rotate(180deg);
                }
                100% {
                    transform: translateY(0) rotate(360deg);
                }
            }
            
            @keyframes pop {
                0% { transform: scale(1); }
                50% { transform: scale(1.5); }
                75% { transform: scale(0.8); }
                100% { transform: scale(0); }
            }
            
            .login-container {
                background: #1e1e1e;
                border-radius: 8px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                padding: 2rem;
                width: 350px;
                text-align: center;
                position: relative;
                z-index: 2;
                display: flex;
                flex-direction: column;
                align-items: center;
            }
            
            .login-container::before {
                content: "";
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: url('/weird-dude') center/100px repeat;
                opacity: 0.1;
                z-index: -1;
                border-radius: 8px;
            }
            
            h1 {
                margin-top: 0;
                color: #bb86fc;
                font-size: 1.8rem;
                position: relative;
                width: 100%;
                text-align: center;
            }
            
            h1::before {
                content: "";
                display: block;
                width: 40px;
                height: 40px;
                background: url('/weird-dude') center/contain no-repeat;
                position: absolute;
                left: -50px;
                top: 50%;
                transform: translateY(-50%);
            }
            
            h1::after {
                content: "";
                display: block;
                width: 40px;
                height: 40px;
                background: url('/weird-dude') center/contain no-repeat;
                position: absolute;
                right: -50px;
                top: 50%;
                transform: translateY(-50%);
            }
            
            .twitch-login-btn {
                background: #9146FF;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 0.8rem 1.5rem;
                font-size: 1rem;
                font-weight: bold;
                cursor: pointer;
                transition: background-color 0.3s;
                width: 100%;
                display: flex;
                justify-content: center;
                align-items: center;
                text-decoration: none;
                box-sizing: border-box;
                margin: 0 auto;
            }
            
            .twitch-login-btn:hover {
                background: #7d30e7;
            }
            
            .twitch-login-btn svg {
                margin-right: 8px;
            }
            
            .footer-emote-container {
                margin-top: 30px;
                display: flex;
                justify-content: space-between;
                width: 100%;
            }
            
            .footer-emote {
                cursor: grab;
                transition: transform 0.3s ease;
            }
            
            .footer-emote:hover {
                transform: scale(1.1);
            }
            
            .footer {
                position: absolute;
                bottom: 10px;
                left: 0;
                right: 0;
                text-align: center;
                font-size: 0.9rem;
                color: #888;
                padding: 10px;
                z-index: 5;
            }
            
            /* Ensure emotes don't overlap footer */
            .emote {
                max-bottom: calc(100vh - 40px);
            }
            
            .alert {
                padding: 0.8rem;
                margin-bottom: 1.5rem;
                border-radius: 4px;
                background: #cf6679;
                color: white;
                width: 100%;
                box-sizing: border-box;
            }
            
            #follower {
                position: fixed; 
                width: 32px; 
                height: 32px; 
                pointer-events: none; 
                z-index: 9999;
                /* Initial position off-screen to prevent flickering on page load */
                left: -100px;
                top: -100px;
                opacity: 0;
                transition: opacity 0.3s ease;
            }
        </style>
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                // Create 30 random emotes in the background
                const body = document.body;
                for(let i = 0; i < 30; i++) {
                    createEmote(body);
                }
                
                // Initialize draggable functionality for all emotes
                initDraggableEmotes();
                
                // Make all footer emotes draggable too and set random sizes
                document.querySelectorAll('.footer-emote').forEach(emote => {
                    // Randomize size between 30px and 60px
                    const size = Math.floor(30 + Math.random() * 30);
                    emote.style.width = size + 'px';
                    emote.style.height = size + 'px';
                    
                    makeElementDraggable(emote);
                    setupClickCounter(emote);
                });
                
                // Setup hydrophobic effect for mouse movements
                setupHydrophobicEffect();
                
                // Make weird dude emote follow cursor
                const follower = document.getElementById('follower');
                
                // Wait a short time before enabling the follower to prevent initial flicker
                setTimeout(() => {
                    if (follower) {
                        follower.style.opacity = '1';
                        
                        document.addEventListener('mousemove', function(e) {
                            follower.style.left = (e.clientX - 16) + 'px';
                            follower.style.top = (e.clientY - 16) + 'px';
                        });
                    }
                }, 500);
            });
            
            // Track mouse position for hydrophobic effect
            let mouseX = 0;
            let mouseY = 0;
            const REPEL_DISTANCE = 150; // Max distance for repel effect
            const REPEL_STRENGTH = 0.5; // Strength of repel effect (0-1)
            
            function setupHydrophobicEffect() {
                document.addEventListener('mousemove', function(e) {
                    mouseX = e.clientX;
                    mouseY = e.clientY;
                    
                    // Apply repel effect to all emotes
                    document.querySelectorAll('.emote').forEach(emote => {
                        if (!emote.classList.contains('dragging')) {
                            applyRepelEffect(emote);
                        }
                    });
                    
                    document.querySelectorAll('.footer-emote').forEach(emote => {
                        if (!emote.classList.contains('dragging')) {
                            applyRepelEffect(emote);
                        }
                    });
                });
            }
            
            function applyRepelEffect(element) {
                const rect = element.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                
                // Calculate distance from mouse to element center
                const dx = centerX - mouseX;
                const dy = centerY - mouseY;
                const distance = Math.sqrt(dx * dx + dy * dy);
                
                // Only apply effect within the REPEL_DISTANCE
                if (distance < REPEL_DISTANCE) {
                    // Calculate repel force (stronger when closer)
                    const force = 1 - (distance / REPEL_DISTANCE);
                    
                    // Calculate repel vector
                    const moveX = dx * force * REPEL_STRENGTH;
                    const moveY = dy * force * REPEL_STRENGTH;
                    
                    // Apply transform - using translate to avoid interfering with other animations
                    element.style.transform = `translate(${moveX}px, ${moveY}px)`;
                    
                    // Reset transform after delay to allow for fluid movement
                    setTimeout(() => {
                        element.style.transform = '';
                    }, 500);
                }
            }
            
            function createEmote(parent) {
                const emote = document.createElement('img');
                emote.src = '/weird-dude';
                emote.className = 'emote';
                
                // Assign a random size between 32px and 80px
                const sizeMin = 32;
                const sizeMax = 80;
                const size = Math.floor(sizeMin + Math.random() * (sizeMax - sizeMin));
                
                emote.style.width = size + 'px';
                emote.style.height = size + 'px';
                
                // Get viewport dimensions
                const viewportWidth = window.innerWidth;
                const viewportHeight = window.innerHeight;
                
                // Calculate random position within viewport boundaries with margin
                const maxLeft = viewportWidth - size - 10;
                const maxTop = viewportHeight - size - 50; // Account for footer
                
                // Set position within safe boundaries
                emote.style.left = (Math.random() * maxLeft) + 'px';
                emote.style.top = (Math.random() * maxTop) + 'px';
                
                // Randomize animation duration and delay
                emote.style.animationDuration = (5 + Math.random() * 15) + 's';
                emote.style.animationDelay = Math.random() * 5 + 's';
                emote.style.transform = 'rotate(' + (Math.random() * 360) + 'deg)';
                
                parent.appendChild(emote);
                
                // Make emote draggable
                makeElementDraggable(emote);
                
                // Setup click counter for pop effect
                setupClickCounter(emote);
            }
            
            function initDraggableEmotes() {
                document.querySelectorAll('.emote').forEach(emote => {
                    makeElementDraggable(emote);
                });
            }
            
            function makeElementDraggable(element) {
                let isDragging = false;
                let offsetX, offsetY;
                
                element.addEventListener('mousedown', startDrag);
                
                function startDrag(e) {
                    isDragging = true;
                    element.classList.add('dragging');
                    
                    // Calculate the offset from mouse position to element corner
                    const rect = element.getBoundingClientRect();
                    offsetX = e.clientX - rect.left;
                    offsetY = e.clientY - rect.top;
                    
                    // Stop animation while dragging
                    element.style.animation = 'none';
                    
                    // Add event listeners for drag and end
                    document.addEventListener('mousemove', drag);
                    document.addEventListener('mouseup', endDrag);
                    
                    // Prevent default behavior
                    e.preventDefault();
                }
                
                function drag(e) {
                    if (isDragging) {
                        // Calculate new position
                        let x = e.clientX - offsetX;
                        let y = e.clientY - offsetY;
                        
                        // Get element dimensions
                        const rect = element.getBoundingClientRect();
                        const width = rect.width;
                        const height = rect.height;
                        
                        // Get viewport dimensions
                        const viewportWidth = window.innerWidth;
                        const viewportHeight = window.innerHeight;
                        
                        // Apply boundaries to keep element fully within viewport
                        x = Math.max(0, Math.min(x, viewportWidth - width));
                        y = Math.max(0, Math.min(y, viewportHeight - height - 40)); // Account for footer
                        
                        // Set the new position
                        element.style.left = x + 'px';
                        element.style.top = y + 'px';
                        
                        // Prevent page scrolling
                        e.preventDefault();
                    }
                }
                
                function endDrag() {
                    if (isDragging) {
                        isDragging = false;
                        element.classList.remove('dragging');
                        
                        // Restore animation if it's a floating emote
                        if (element.classList.contains('emote')) {
                            const duration = (5 + Math.random() * 15) + 's';
                            element.style.animationDuration = duration;
                            element.style.animation = `float ${duration} infinite linear`;
                        }
                        
                        // Remove event listeners
                        document.removeEventListener('mousemove', drag);
                        document.removeEventListener('mouseup', endDrag);
                    }
                }
            }
            
            function setupClickCounter(element) {
                let clickCount = 0;
                let lastClickTime = 0;
                const CLICK_TIMEOUT = 800; // Time in ms within which clicks must occur to count as rapid
                
                element.addEventListener('click', function(e) {
                    // Only count click if not dragging
                    if (!element.classList.contains('dragging')) {
                        const now = Date.now();
                        
                        // Check if this is a rapid click (within CLICK_TIMEOUT ms of last click)
                        if (now - lastClickTime < CLICK_TIMEOUT) {
                            clickCount++;
                        } else {
                            // Reset counter if too much time has passed
                            clickCount = 1;
                        }
                        
                        // Update last click time
                        lastClickTime = now;
                        
                        // Scale up slightly on each click (using transform directly rather than style.transform)
                        const scale = 1 + (clickCount * 0.1);
                        
                        // Apply the transform without affecting position
                        if (element.style.transform && element.style.transform.includes('rotate')) {
                            // If it has rotation, preserve it
                            const rotateVal = element.style.transform.match(/rotate\([^)]+\)/);
                            if (rotateVal) {
                                element.style.transform = `scale(${scale}) ${rotateVal[0]}`;
                            } else {
                                element.style.transform = `scale(${scale})`;
                            }
                        } else {
                            element.style.transform = `scale(${scale})`;
                        }
                        
                        // Pop and remove when clicked enough times rapidly
                        if (clickCount >= 5) {
                            element.style.animation = 'pop 0.5s forwards';
                            
                            // Remove element after animation
                            setTimeout(() => {
                                element.remove();
                                
                                // Create a new emote elsewhere if it was a floating one
                                if (element.classList.contains('emote')) {
                                    createEmote(document.body);
                                }
                            }, 500);
                        }
                    }
                    // Prevent event from bubbling if it was a click
                    if (!element.classList.contains('dragging')) {
                        e.stopPropagation();
                    }
                });
            }
        </script>
    </head>
    <body>
        <div id="follower">
            <img src="/weird-dude" style="width: 100%; height: 100%;">
        </div>
        
        <div class="login-container">
            <h1>mr_adam420 AI Control Panel</h1>
            
            {% if error %}
            <div class="alert">{{ error }}</div>
            {% endif %}
            
            <a href="{{ oauth_url }}" class="twitch-login-btn">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path fill-rule="evenodd" clip-rule="evenodd" d="M2.149 0L0.5 4.5V20.5H6.5V24H10L13.5 20.5H18L23.5 15V0H2.149ZM21.5 14L18 17.5H12.5L9 21V17.5H4.5V2H21.5V14Z" fill="white"/>
                    <path d="M19 5H17V11H19V5Z" fill="white"/>
                    <path d="M13 5H11V11H13V5Z" fill="white"/>
                </svg>
                Log in with Twitch
            </a>
            
            <div class="footer-emote-container">
                <img src="/weird-dude" class="footer-emote">
                <img src="/weird-dude" class="footer-emote">
                <img src="/weird-dude" class="footer-emote">
            </div>
        </div>
        
        <div class="footer">
            made by ShadowDog using AI
        </div>
    </body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!doctype html>
<html>
    <head>
        <title>mr_adam420 AI Control Panel</title>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #121212;
                color: #e0e0e0;
                margin: 0;
                padding: 0;
                line-height: 1.6;
                position: relative;
                min-height: 100vh;
                padding-bottom: 60px; /* Space for footer */
            }
            header {
                background: #1e1e1e;
                color: #bb86fc;
                padding: 1rem 2rem;
                display: flex;
                justify-content: space-between;
                align-items: center;
                box-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
            }
            h1 {
                margin: 0;
                font-size: 1.8rem;
            }
            .user-info {
                display: flex;
                align-items: center;
                gap: 20px;
            }
            .user-badge {
                background: #9146FF;
                color: white;
                font-size: 0.8rem;
                padding: 0.3rem 0.6rem;
                border-radius: 4px;
                font-weight: bold;
                display: inline-block;
                margin-left: 10px;
            }
            .logout-btn {
                background: #cf6679;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 0.5rem 1rem;
                font-size: 0.9rem;
                cursor: pointer;
                text-decoration: none;
            }
            .logout-btn:hover {
                background: #b55b68;
            }
            .container {
                max-width: 1200px;
                margin: 2rem auto;
                padding: 0 1rem;
                display: grid;
                grid-template-columns: 1fr 1fr;
                grid-gap: 2rem;
            }
            @media (max-width: 768px) {
                .container {
                    grid-template-columns: 1fr;
                }
            }
            .card {
                background: #1e1e1e;
                border-radius: 8px;
                box-shadow: 0 4px 10px rgba(0, 0, 0, 0.2);
                padding: 1.5rem;
                margin-bottom: 1.5rem;
            }
            h2 {
                margin-top: 0;
                margin-bottom: 1.2rem;
                color: #bb86fc;
                font-size: 1.5rem;
                border-bottom: 1px solid #333;
                padding-bottom: 0.5rem;
            }
            .form-group {
                margin-bottom: 1.2rem;
                position: relative;
            }
            .form-group label {
                display: block;
                margin-bottom: 0.5rem;
                color: #bb86fc;
            }
            input[type="text"] {
                width: 100%;
                padding: 0.8rem;
                border: none;
                border-radius: 4px;
                background: #2d2d2d;
                color: #e0e0e0;
                font-size: 1rem;
                box-sizing: border-box;
            }
            button {
                background: #bb86fc;
                color: #121212;
                border: none;
                border-radius: 4px;
                padding: 0.7rem 1.2rem;
                font-size: 0.9rem;
                font-weight: bold;
                cursor: pointer;
                transition: background-color 0.3s;
            }
            button:hover {
                background: #a370d8;
            }
            .user-list {
                list-style: none;
                padding: 0;
                margin: 0;
            }
            .user-list li {
                padding: 0.8rem;
                border-bottom: 1px solid #333;
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .user-list li:last-child {
                border-bottom: none;
            }
            .search-results {
                margin-top: 1.5rem;
                padding-top: 1rem;
                border-top: 1px solid #333;
            }
            .alert {
                padding: 0.8rem;
                margin-bottom: 1.5rem;
                border-radius: 4px;
            }
            .alert-success {
                background: rgba(0, 200, 83, 0.2);
                color: #00c853;
                border: 1px solid #00c853;
            }
            .alert-error {
                background: rgba(207, 102, 121, 0.2);
                color: #cf6679;
                border: 1px solid #cf6679;
            }
            .footer {
                position: absolute;
                bottom: 0;
                left: 0;
                right: 0;
                text-align: center;
                font-size: 0.9rem;
                color: #888;
                padding: 10px;
                z-index: 5;
            }
            .autocomplete-items {
                position: absolute;
                border-radius: 0 0 4px 4px;
                z-index: 99;
                top: 100%;
                left: 0;
                right: 0;
                background-color: #2d2d2d;
                box-shadow: 0 4px 10px rgba(0, 0, 0, 0.3);
            }
            .autocomplete-items div {
                padding: 10px;
                cursor: pointer;
                border-bottom: 1px solid #333;
                color: #e0e0e0;
            }
            .autocomplete-items div:hover {
                background-color: #3a3a3a;
            }
            .autocomplete-active {
                background-color: #3a3a3a !important;
            }
            
            /* Welcome popup */
            .welcome-overlay {
                position: fixed;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background: rgba(0, 0, 0, 0.8);
                display: flex;
                justify-content: center;
                align-items: center;
                z-index: 9999;
            }
            
            .welcome-popup {
                background: #1e1e1e;
                border-radius: 8px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                padding: 2rem;
                width: 400px;
                text-align: center;
                position: relative;
                animation: welcome-fade-in 0.5s ease-out;
            }
            
            @keyframes welcome-fade-in {
                0% { opacity: 0; transform: translateY(-20px); }
                100% { opacity: 1; transform: translateY(0); }
            }
            
            .welcome-popup h2 {
                color: #bb86fc;
                margin-top: 0;
                border-bottom: none;
                font-size: 2rem;
            }
            
            .welcome-popup p {
                margin-bottom: 1.5rem;
                font-size: 1.1rem;
            }
            
            .welcome-popup img {
                width: 100px;
                height: 100px;
                margin: 1rem auto;
                display: block;
            }
            
            .welcome-popup button {
                background: #bb86fc;
                color: #121212;
                border: none;
                border-radius: 4px;
                padding: 0.8rem 1.5rem;
                font-size: 1rem;
                font-weight: bold;
                cursor: pointer;
                transition: background-color 0.3s;
                width: 100%;
            }
            
            .welcome-popup button:hover {
                background: #a370d8;
            }
        </style>
        <script>
            function autocomplete(inp, arr) {
                var currentFocus;
                
                inp.addEventListener("input", function(e) {
                    var a, b, i, val = this.value.toLowerCase();
                    closeAllLists();
                    if (!val) { return false; }
                    currentFocus = -1;
                    a = document.createElement("DIV");
                    a.setAttribute("id", this.id + "autocomplete-list");
                    a.setAttribute("class", "autocomplete-items");
                    this.parentNode.appendChild(a);
                    
                    let matchCount = 0;
                    const maxMatches = 10;  // Limit results to prevent overwhelming UI
                    
                    for (i = 0; i < arr.length; i++) {
                        if (arr[i].toLowerCase().indexOf(val) > -1 && matchCount < maxMatches) {
                            matchCount++;
                            b = document.createElement("DIV");
                            b.innerHTML = arr[i].replace(new RegExp(val, "gi"), "<strong>$&</strong>");
                            b.innerHTML += "<input type='hidden' value='" + arr[i] + "'>";
                            b.addEventListener("click", function(e) {
                                inp.value = this.getElementsByTagName("input")[0].value;
                                closeAllLists();
                            });
                            a.appendChild(b);
                        }
                    }
                });
                
                inp.addEventListener("keydown", function(e) {
                    var x = document.getElementById(this.id + "autocomplete-list");
                    if (x) x = x.getElementsByTagName("div");
                    if (e.keyCode == 40) { // Down key
                        currentFocus++;
                        addActive(x);
                    } else if (e.keyCode == 38) { // Up key
                        currentFocus--;
                        addActive(x);
                    } else if (e.keyCode == 13) { // Enter key
                        e.preventDefault();
                        if (currentFocus > -1) {
                            if (x) x[currentFocus].click();
                        }
                    }
                });
                
                function addActive(x) {
                    if (!x) return false;
                    removeActive(x);
                    if (currentFocus >= x.length) currentFocus = 0;
                    if (currentFocus < 0) currentFocus = (x.length - 1);
                    x[currentFocus].classList.add("autocomplete-active");
                }
                
                function removeActive(x) {
                    for (var i = 0; i < x.length; i++) {
                        x[i].classList.remove("autocomplete-active");
                    }
                }
                
                function closeAllLists(elmnt) {
                    var x = document.getElementsByClassName("autocomplete-items");
                    for (var i = 0; i < x.length; i++) {
                        if (elmnt != x[i] && elmnt != inp) {
                            x[i].parentNode.removeChild(x[i]);
                        }
                    }
                }
                
                document.addEventListener("click", function (e) {
                    closeAllLists(e.target);
                });
            }
            
            function closeWelcomePopup() {
                const popup = document.getElementById('welcome-overlay');
                if (popup) {
                    popup.style.opacity = '0';
                    setTimeout(() => {
                        popup.remove();
                    }, 500);
                }
            }
            
            // Initialize autocomplete when DOM is loaded
            document.addEventListener('DOMContentLoaded', function() {
                // Get whitelist users from the template
                var whitelistUsers = {{ whitelist_json|safe }};
                var moderatorUsers = {{ moderators_json|safe }};
                
                // Setup autocomplete for both search and remove fields
                autocomplete(document.getElementById("search-username"), whitelistUsers);
                autocomplete(document.getElementById("remove-username"), whitelistUsers);
                
                // Setup autocomplete for moderator removal field
                var removeModField = document.getElementById("remove-mod-username");
                if (removeModField) {
                    autocomplete(removeModField, moderatorUsers);
                }
                
                // Check if we should show welcome popup
                const shouldShowWelcome = {{ 'true' if show_welcome else 'false' }};
                if (shouldShowWelcome) {
                    // Auto-close the welcome popup after 5 seconds
                    setTimeout(closeWelcomePopup, 5000);
                }
            });
        </script>
    </head>
    <body>
        {% if show_welcome %}
        <div id="welcome-overlay" class="welcome-overlay">
            <div class="welcome-popup">
                <img src="/weird-dude" alt="WeirdDude">
                <h2>Welcome, {{ twitch_display_name }}!</h2>
                <p>You've successfully logged in to the mr_adam420 AI Control Panel.</p>
                <p>Role: <strong>{{ user_role|capitalize }}</strong></p>
                <button onclick="closeWelcomePopup()">Get Started</button>
            </div>
        </div>
        {% endif %}
        
        <header>
            <h1>mr_adam420 AI Control Panel</h1>
            <div class="user-info">
                <div>
                    <span>{{ twitch_display_name }}</span>
                    <span class="user-badge">{{ user_role|capitalize }}</span>
                </div>
                <a href="{{ url_for('logout') }}" class="logout-btn">Logout</a>
            </div>
        </header>
        
        <div class="container">
            <div class="left-column">
                {% if message %}
                <div class="alert {% if error %}alert-error{% else %}alert-success{% endif %}">
                    {{ message }}
                </div>
                {% endif %}
                
                <div class="card">
                    <h2>Add User</h2>
                    <form method="post" action="{{ url_for('add_user') }}" autocomplete="off">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <div class="form-group">
                            <label for="add-username">Twitch Username</label>
                            <input type="text" id="add-username" name="username_{{ range(1000, 9999) | random }}" placeholder="Enter Twitch username" autocomplete="new-password" required>
                            <input type="hidden" name="username" id="real-add-username">
                        </div>
                        <button type="submit" onclick="document.getElementById('real-add-username').value = document.getElementById('add-username').value; return true;">Add User</button>
                    </form>
                </div>
                
                <div class="card">
                    <h2>Remove User</h2>
                    <form method="post" action="{{ url_for('remove_user') }}" autocomplete="off">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <div class="form-group">
                            <label for="remove-username">Twitch Username</label>
                            <input type="text" id="remove-username" name="username_{{ range(1000, 9999) | random }}" placeholder="Enter Twitch username" autocomplete="new-password" required>
                            <input type="hidden" name="username" id="real-remove-username">
                        </div>
                        <button type="submit" onclick="document.getElementById('real-remove-username').value = document.getElementById('remove-username').value; return true;">Remove User</button>
                    </form>
                </div>
                
                <div class="card">
                    <h2>Search User</h2>
                    <form method="get" action="{{ url_for('search_user') }}" autocomplete="off">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <div class="form-group">
                            <label for="search-username">Twitch Username</label>
                            <input type="text" id="search-username" name="query_{{ range(1000, 9999) | random }}" placeholder="Enter search term" autocomplete="new-password" required>
                            <input type="hidden" name="query" id="real-query">
                        </div>
                        <button type="submit" onclick="document.getElementById('real-query').value = document.getElementById('search-username').value; return true;">Search</button>
                    </form>
                    
                    {% if search_results is defined and search_results %}
                    <div class="search-results">
                        <h3>Search Results:</h3>
                        <ul class="user-list">
                            {% for user in search_results %}
                            <li>{{ user }}</li>
                            {% endfor %}
                        </ul>
                    </div>
                    {% elif search_results is defined %}
                    <div class="search-results">
                        <p>No results found.</p>
                    </div>
                    {% endif %}
                </div>
            </div>
            
            <div class="right-column">
                <div class="card">
                    <h2>System Prompt</h2>
                    <form method="post" action="{{ url_for('update_system_prompt') }}">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <div class="form-group" style="margin-bottom: 0.5rem;">
                            <label>Safety Rule (Cannot Be Edited):</label>
                            <div style="width: 100%; padding: 0.8rem; border-radius: 4px; background: #1a1a1a; color: #cf6679; font-size: 1rem; box-sizing: border-box; font-weight: bold; margin-bottom: 1rem;">{{ safety_rule }}</div>
                        </div>
                        <div class="form-group">
                            <label for="system-prompt">Editable System Prompt:</label>
                            <textarea id="system-prompt" name="system_prompt" rows="8" style="width: 100%; padding: 0.8rem; border: none; border-radius: 4px; background: #2d2d2d; color: #e0e0e0; font-size: 1rem; box-sizing: border-box; resize: vertical; white-space: pre-wrap;">{{ current_system_prompt }}</textarea>
                        </div>
                        <p style="color: #e0e0e0; font-size: 0.85rem; margin-bottom: 1rem;">
                            Note: The safety rule above will always be included in the final prompt. Updating the system prompt will restart the bot to apply changes.
                        </p>
                        <button type="submit">Update System Prompt</button>
                    </form>
                </div>
                
                {% if user_role == 'owner' %}
                <div class="card">
                    <h2>Manage Moderators</h2>
                    <form method="post" action="{{ url_for('add_moderator') }}" autocomplete="off">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <div class="form-group">
                            <label for="add-mod-username">Add Moderator</label>
                            <input type="text" id="add-mod-username" name="username_{{ range(1000, 9999) | random }}" placeholder="Enter Twitch username" autocomplete="new-password" required>
                            <input type="hidden" name="username" id="real-add-mod-username">
                        </div>
                        <button type="submit" onclick="document.getElementById('real-add-mod-username').value = document.getElementById('add-mod-username').value; return true;">Add Moderator</button>
                    </form>
                    
                    <form method="post" action="{{ url_for('remove_moderator') }}" style="margin-top: 20px;" autocomplete="off">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                        <div class="form-group">
                            <label for="remove-mod-username">Remove Moderator</label>
                            <input type="text" id="remove-mod-username" name="username_{{ range(1000, 9999) | random }}" placeholder="Enter Twitch username" autocomplete="new-password" required>
                            <input type="hidden" name="username" id="real-remove-mod-username">
                        </div>
                        <button type="submit" onclick="document.getElementById('real-remove-mod-username').value = document.getElementById('remove-mod-username').value; return true;">Remove Moderator</button>
                    </form>
                    
                    <div style="margin-top: 20px;">
                        <h3>Current Moderators:</h3>
                        {% if moderators %}
                        <ul class="user-list">
                            {% for mod in moderators %}
                            <li>{{ mod }}</li>
                            {% endfor %}
                        </ul>
                        {% else %}
                        <p>No moderators configured.</p>
                        {% endif %}
                    </div>
                </div>
                {% endif %}
                
                <div class="card">
                    <h2>Whitelisted Users</h2>
                    {% if whitelist %}
                    <ul class="user-list">
                        {% for user in whitelist %}
                        <li>{{ user }}</li>
                        {% endfor %}
                    </ul>
                    {% else %}
                    <p>No users are currently whitelisted.</p>
                    {% endif %}
                </div>
            </div>
        </div>

        <div class="footer">
            made by ShadowDog using AI
        </div>
    </body>
</html>
"""

# Print startup information about LM Studio
print("=" * 50)
print("ADAM AI CHAT BOT - LM STUDIO EDITION")
print("=" * 50)
print(f"Using LM Studio API at: {LM_STUDIO_API_URL}")
print(f"Using model: {LM_STUDIO_MODEL}")
print("\nIMPORTANT: Make sure LM Studio is running with the server enabled!")
print("1. Open LM Studio")
print("2. Go to the 'Developer' tab")
print("3. Select your model and click 'Start Server'")
print("=" * 50)

# Security middleware for CSRF protection
def generate_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
    return session['csrf_token']

# Authentication decorator
def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if user is authenticated
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.url))
        
        # Check if session is expired
        if 'last_activity' in session and time.time() - session['last_activity'] > SESSION_TIMEOUT:
            session.clear()
            return redirect(url_for('login', next=request.url))
        
        # Update last activity time
        session['last_activity'] = time.time()
        
        # Check user role
        if not is_user_authorized():
            return render_template_string("""
                <!doctype html>
                <html>
                    <head>
                        <title>Access Denied</title>
                        <style>
                            body {
                                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                                background: #121212;
                                color: #e0e0e0;
                                margin: 0;
                                padding: 0;
                                display: flex;
                                justify-content: center;
                                align-items: center;
                                height: 100vh;
                            }
                            .error-container {
                                background: #1e1e1e;
                                border-radius: 8px;
                                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                                padding: 2rem;
                                width: 350px;
                                text-align: center;
                            }
                            h1 {
                                color: #cf6679;
                                margin-top: 0;
                            }
                            a {
                                color: #bb86fc;
                                text-decoration: none;
                            }
                            a:hover {
                                text-decoration: underline;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="error-container">
                            <h1>Access Denied</h1>
                            <p>You don't have permission to access this page.</p>
                            <p><a href="/">Return to Homepage</a></p>
                        </div>
                    </body>
                </html>
            """)
        
        return f(*args, **kwargs)
    return decorated_function

def owner_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if user is authenticated and is an owner
        if not session.get('authenticated'):
            return redirect(url_for('login', next=request.url))
        
        # Check if user is an owner
        if session.get('twitch_username', '').lower() not in [owner.lower() for owner in BOT_OWNERS]:
            return render_template_string("""
                <!doctype html>
                <html>
                    <head>
                        <title>Owner Access Only</title>
                        <style>
                            body {
                                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                                background: #121212;
                                color: #e0e0e0;
                                margin: 0;
                                padding: 0;
                                display: flex;
                                justify-content: center;
                                align-items: center;
                                height: 100vh;
                            }
                            .error-container {
                                background: #1e1e1e;
                                border-radius: 8px;
                                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                                padding: 2rem;
                                width: 350px;
                                text-align: center;
                            }
                            h1 {
                                color: #cf6679;
                                margin-top: 0;
                            }
                            a {
                                color: #bb86fc;
                                text-decoration: none;
                            }
                            a:hover {
                                text-decoration: underline;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="error-container">
                            <h1>Owner Access Only</h1>
                            <p>This page is restricted to bot owners only.</p>
                            <p><a href="/dashboard">Return to Dashboard</a></p>
                        </div>
                    </body>
                </html>
            """)
        
        return f(*args, **kwargs)
    return decorated_function

def is_user_authorized():
    """Check if the current user is authorized to access the control panel"""
    if not session.get('authenticated'):
        return False
        
    username = session.get('twitch_username', '').lower()
    
    # Allow access if user is an owner
    if username in [owner.lower() for owner in BOT_OWNERS]:
        return True
        
    # Allow access if user is a moderator
    if username in [mod.lower() for mod in moderators]:
        return True
        
    return False

def get_user_role():
    """Get the role of the current user"""
    if not session.get('authenticated'):
        return "guest"
        
    username = session.get('twitch_username', '').lower()
    
    if username in [owner.lower() for owner in BOT_OWNERS]:
        return "owner"
        
    if username in [mod.lower() for mod in moderators]:
        return "moderator"
        
    return "guest"

# Authentication routes
@app.route("/login")
def login():
    # Generate a random state string for security
    state = secrets.token_hex(16)
    session['oauth_state'] = state
    
    # Build the Twitch OAuth URL
    auth_url = "https://id.twitch.tv/oauth2/authorize"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope": TWITCH_SCOPES,
        "state": state
    }
    oauth_url = f"{auth_url}?{urllib.parse.urlencode(params)}"
    
    return render_template_string(TWITCH_LOGIN_TEMPLATE, oauth_url=oauth_url)

@app.route("/auth/callback")
def auth_callback():
    # Verify state parameter to prevent CSRF
    if request.args.get('state') != session.get('oauth_state'):
        flash("Invalid auth state, please try again.")
        return redirect(url_for('login'))
    
    # Check for error
    if 'error' in request.args:
        flash(f"Authentication error: {request.args.get('error')}")
        return redirect(url_for('login'))
    
    # Get authorization code
    code = request.args.get('code')
    if not code:
        flash("No authorization code provided.")
        return redirect(url_for('login'))
    
    # Exchange code for access token
    token_url = "https://id.twitch.tv/oauth2/token"
    token_params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TWITCH_REDIRECT_URI
    }
    
    try:
        token_response = requests.post(token_url, data=token_params)
        token_data = token_response.json()
        
        if 'access_token' not in token_data:
            flash(f"Failed to get access token: {token_data.get('message', 'Unknown error')}")
            return redirect(url_for('login'))
        
        # Save the access token for potential revocation on logout
        session['twitch_access_token'] = token_data['access_token']
        
        # Get user info with access token
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token_data['access_token']}"
        }
        user_response = requests.get("https://api.twitch.tv/helix/users", headers=headers)
        user_data = user_response.json()
        
        if 'data' not in user_data or not user_data['data']:
            flash("Failed to get user information.")
            return redirect(url_for('login'))
        
        # Get username
        twitch_username = user_data['data'][0]['login']
        twitch_display_name = user_data['data'][0]['display_name']
        
        # Check if user is authorized
        is_owner = twitch_username.lower() in [owner.lower() for owner in BOT_OWNERS]
        is_moderator = twitch_username.lower() in [mod.lower() for mod in moderators]
        
        if not is_owner and not is_moderator:
            return render_template_string("""
                <!doctype html>
                <html>
                    <head>
                        <title>Access Denied</title>
                        <style>
                            body {
                                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                                background: #121212;
                                color: #e0e0e0;
                                margin: 0;
                                padding: 0;
                                display: flex;
                                justify-content: center;
                                align-items: center;
                                height: 100vh;
                            }
                            .error-container {
                                background: #1e1e1e;
                                border-radius: 8px;
                                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                                padding: 2rem;
                                width: 350px;
                                text-align: center;
                            }
                            h1 {
                                color: #cf6679;
                                margin-top: 0;
                            }
                            a {
                                color: #bb86fc;
                                text-decoration: none;
                            }
                            a:hover {
                                text-decoration: underline;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="error-container">
                            <h1>Access Denied</h1>
                            <p>Sorry, {{ display_name }}, you don't have permission to access the control panel.</p>
                            <p><a href="/">Return to Homepage</a></p>
                        </div>
                    </body>
                </html>
            """, display_name=twitch_display_name)
        
        # Set session variables
        session['authenticated'] = True
        session['twitch_username'] = twitch_username
        session['twitch_display_name'] = twitch_display_name
        session['user_role'] = "owner" if is_owner else "moderator"
        session['last_activity'] = time.time()
        session['user_id'] = str(uuid.uuid4())  # Generate unique session ID
        
        # Store session in active_sessions for tracking
        active_sessions[session['user_id']] = {
            'username': twitch_username,
            'display_name': twitch_display_name,
            'role': session['user_role'],
            'login_time': time.time()
        }
        
        # Show welcome message and redirect to dashboard
        session['show_welcome'] = True
        
        # Redirect directly to dashboard without requiring another click
        return redirect(url_for('dashboard'))
        
    except Exception as e:
        flash(f"Authentication error: {str(e)}")
        return redirect(url_for('login'))

@app.route("/logout")
def logout():
    # Revoke Twitch access token if present
    access_token = session.get('twitch_access_token')
    if access_token:
        try:
            # Call Twitch API to revoke the token
            revoke_url = "https://id.twitch.tv/oauth2/revoke"
            revoke_params = {
                "client_id": TWITCH_CLIENT_ID,
                "token": access_token
            }
            requests.post(revoke_url, data=revoke_params)
        except Exception as e:
            print(f"Error revoking Twitch token: {str(e)}")
    
    # Remove from active sessions if tracked
    if 'user_id' in session and session['user_id'] in active_sessions:
        del active_sessions[session['user_id']]
    
    # Clear all session data
    session.clear()
    return redirect(url_for('login'))

# Secured dashboard route
@app.route("/dashboard")
@login_required
def dashboard():
    # Always read the current system prompt directly from file to ensure we display the latest version
    try:
        if os.path.exists(SYSTEM_PROMPT_FILE):
            with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
                current_prompt = f.read()
                
                # Ensure consistent line endings
                current_prompt = current_prompt.replace('\r\n', '\n')
        else:
            current_prompt = ""
    except Exception as e:
        print(f"Error reading system prompt file: {e}")
        current_prompt = ""
        
    # Get message from session if exists
    message = session.pop('message', None)
    error = session.pop('error', None)
    
    # Remove show_welcome flag after displaying
    show_welcome = session.pop('show_welcome', False)
    
    # Convert lists to JSON for JavaScript
    whitelist_json = json.dumps(sorted(list(whitelist)))
    moderators_json = json.dumps(sorted(moderators))
    
    return render_template_string(DASHBOARD_TEMPLATE,
        message=message,
        whitelist=sorted(list(whitelist)),
        whitelist_json=whitelist_json,
        moderators=sorted(moderators),
        moderators_json=moderators_json,
        csrf_token=generate_csrf_token(),
        error=session.pop('error', None),
        current_system_prompt=current_prompt,  # Only the user-editable part
        safety_rule=SAFETY_RULE,  # Pass the safety rule for display purposes
        show_welcome=show_welcome,
        twitch_display_name=session.get('twitch_display_name', ''),
        user_role=session.get('user_role', 'guest')
    )

# Route to handle the default index page (redirect to dashboard if authenticated)
@app.route("/")
def index():
    if 'authenticated' in session and session['authenticated']:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# Secured user management routes
@app.route("/add", methods=["POST"])
@login_required
def add_user():
    # Verify CSRF token
    if request.form.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    username = request.form.get("username", "").strip().lower()
    
    # Debug the incoming data
    print(f"Add user form data: {request.form}")
    print(f"Parsed username: '{username}'")
    
    if not username:
        session['error'] = True
        session['message'] = "Username cannot be empty. Please enter a valid Twitch username."
        return redirect(url_for('dashboard'))
    
    if username in whitelist:
        session['error'] = True
        session['message'] = f"User '{username}' is already whitelisted."
    else:
        whitelist.add(username)
        save_whitelist()
        session['error'] = False
        session['message'] = f"User '{username}' has been added to the whitelist."
    
    return redirect(url_for('dashboard'))

@app.route("/remove", methods=["POST"])
@login_required
def remove_user():
    # Verify CSRF token
    if request.form.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    username = request.form.get("username", "").strip().lower()
    
    # Debug the incoming data
    print(f"Remove user form data: {request.form}")
    print(f"Parsed username: '{username}'")
    
    if not username:
        session['error'] = True
        session['message'] = "Username cannot be empty. Please enter a valid Twitch username."
        return redirect(url_for('dashboard'))
    
    if username in whitelist:
        whitelist.discard(username)
        save_whitelist()
        session['error'] = False
        session['message'] = f"User '{username}' has been removed from the whitelist."
    else:
        session['error'] = True
        session['message'] = f"User '{username}' is not in the whitelist."
    
    return redirect(url_for('dashboard'))

@app.route("/search", methods=["GET"])
@login_required
def search_user():
    # Verify CSRF token if possible (GET requests might not have this but we'll check anyway)
    if request.args.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    # Get search query
    query = request.args.get("query", "").strip().lower()
    
    # Find matching users
    results = []
    if query:
        for user in whitelist:
            if query in user.lower():
                results.append(user)
    
    # Read current system prompt for display
    try:
        if os.path.exists(SYSTEM_PROMPT_FILE):
            with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
                current_prompt = f.read()
                # Ensure consistent line endings
                current_prompt = current_prompt.replace('\r\n', '\n')
        else:
            current_prompt = ""
    except Exception as e:
        print(f"Error reading system prompt file: {e}")
        current_prompt = ""
    
    # Pass results to template
    return render_template_string(DASHBOARD_TEMPLATE,
        message=session.get('message'),
        whitelist=sorted(list(whitelist)),
        whitelist_json=json.dumps(sorted(list(whitelist))),  # Add JSON list for JavaScript
        moderators=sorted(moderators),
        moderators_json=json.dumps(sorted(moderators)),
        search_results=results,
        csrf_token=generate_csrf_token(),
        error=session.get('error'),
        current_system_prompt=current_prompt,  # Add the current system prompt
        safety_rule=SAFETY_RULE,  # Add the safety rule
        show_welcome=False,  # Don't show welcome on search
        twitch_display_name=session.get('twitch_display_name', ''),
        user_role=session.get('user_role', 'guest')
    )

# Add a route to serve the WeirdDude.png image
@app.route("/weird-dude")
def weird_dude():
    return app.send_static_file("WeirdDude.png")

# Create a static folder if it doesn't exist
import os
if not os.path.exists('static'):
    os.makedirs('static')
    
# Copy WeirdDude.png to the static folder on startup
import shutil
try:
    shutil.copy2('WeirdDude.png', 'static/WeirdDude.png')
except:
    print("Warning: Could not copy WeirdDude.png to static folder")

def run_flask():
    print("\n🔥 Flask server starting on http://0.0.0.0:5420")
    app.run(host='0.0.0.0', port=5420)

# ---------------------------
# Main Execution
# ---------------------------
def run_twitch_bot():
    global should_restart
    
    while True:
        try:
            print("Starting Twitch bot...")
            
            # First create a new event loop and set it as the current loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Now create the bot, which will use the current event loop
            bot = TwitchBot()
            
            # Run the bot in this same loop
            loop.run_until_complete(bot.start())
            loop.close()
        except Exception as e:
            print(f"Twitch bot error: {e}")
        
        # Check if we should restart due to system prompt update
        if should_restart:
            print("Restarting Twitch bot due to system prompt update...")
            should_restart = False  # Reset the flag
            time.sleep(1)  # Small delay before restart
        else:
            # If it's a normal crash/exit, wait before reconnecting
            print("Twitch bot disconnected, reconnecting in 5 seconds...")
            time.sleep(2)

# Route to handle system prompt updates
@app.route("/update_system_prompt", methods=["POST"])
@login_required
def update_system_prompt():
    # Verify CSRF token
    if request.form.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    # Get the new system prompt from the form (this is just the user-editable part)
    new_system_prompt = request.form.get('system_prompt', '')
    
    # Fix line breaks to ensure consistent formatting
    # Replace Windows-style line breaks with Unix-style
    new_system_prompt = new_system_prompt.replace('\r\n', '\n')
    
    # Remove any trailing whitespace on each line while preserving line breaks
    lines = new_system_prompt.split('\n')
    lines = [line.rstrip() for line in lines]
    new_system_prompt = '\n'.join(lines)
    
    if not new_system_prompt.strip():
        session['error'] = True
        session['message'] = "System prompt cannot be empty."
        return redirect(url_for('dashboard'))
    
    try:
        # Write only the user-editable part to the file
        with open(SYSTEM_PROMPT_FILE, "w", encoding="utf-8") as f:
            f.write(new_system_prompt)
        
        # Set a success message
        session['message'] = "System prompt updated successfully. The server will restart momentarily."
        
        # Create a daemon thread that will wait before restarting
        restart_thread = threading.Thread(target=restart_server, daemon=True)
        restart_thread.start()
        
        # Redirect to dashboard to show the updated prompt
        return redirect(url_for('dashboard'))
    except Exception as e:
        session['error'] = True
        session['message'] = f"Error updating system prompt: {str(e)}"
        return redirect(url_for('dashboard'))

# Function to restart the server
def restart_server():
    # Wait longer to ensure the page has time to fully load and display the updated prompt
    print("Scheduling bot restart in 5 seconds...")
    time.sleep(2)
    
    # Update the global system prompt variable
    global system_prompt
    try:
        with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
            system_prompt = f.read() or "You are a helpful assistant."  # Removed .strip() to preserve formatting
        # Always reapply the safety rule
        system_prompt = SAFETY_RULE + system_prompt
        print("System prompt updated to:", system_prompt)
    except Exception as e:
        print(f"Error reading system prompt during restart: {e}")
        return
    
    print("Restarting bot threads...")
    
    # Signal to restart the Twitch bot thread
    global should_restart
    should_restart = True

# Moderator management routes
@app.route("/add_moderator", methods=["POST"])
@owner_required
def add_moderator():
    # Verify CSRF token
    if request.form.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    username = request.form.get("username", "").strip().lower()
    
    if not username:
        session['error'] = True
        session['message'] = "Username cannot be empty."
        return redirect(url_for('dashboard'))
    
    if username in moderators:
        session['message'] = f"{username} is already a moderator."
        return redirect(url_for('dashboard'))
    
    # Add to moderators list
    moderators.append(username)
    save_moderators(moderators)
    
    session['message'] = f"{username} has been added as a moderator."
    return redirect(url_for('dashboard'))

@app.route("/remove_moderator", methods=["POST"])
@owner_required
def remove_moderator():
    # Verify CSRF token
    if request.form.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    username = request.form.get("username", "").strip().lower()
    
    if not username:
        session['error'] = True
        session['message'] = "Username cannot be empty."
        return redirect(url_for('dashboard'))
    
    if username not in moderators:
        session['error'] = True
        session['message'] = f"{username} is not a moderator."
        return redirect(url_for('dashboard'))
    
    # Remove from moderators list
    moderators.remove(username)
    save_moderators(moderators)
    
    session['message'] = f"{username} has been removed as a moderator."
    return redirect(url_for('dashboard'))

# Route to view active sessions (owners only)
@app.route("/sessions")
@owner_required
def active_sessions_view():
    return render_template_string("""
        <!doctype html>
        <html>
            <head>
                <title>Active Sessions</title>
                <style>
                    body {
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                        background: #121212;
                        color: #e0e0e0;
                        margin: 0;
                        padding: 0;
                        line-height: 1.6;
                    }
                    .container {
                        max-width: 800px;
                        margin: 2rem auto;
                        padding: 0 1rem;
                    }
                    .card {
                        background: #1e1e1e;
                        border-radius: 8px;
                        box-shadow: 0 4px 10px rgba(0, 0, 0, 0.2);
                        padding: 1.5rem;
                        margin-bottom: 1.5rem;
                    }
                    h1 {
                        color: #bb86fc;
                        margin-top: 0;
                    }
                    table {
                        width: 100%;
                        border-collapse: collapse;
                    }
                    th, td {
                        padding: 0.8rem;
                        text-align: left;
                        border-bottom: 1px solid #333;
                    }
                    th {
                        color: #bb86fc;
                    }
                    .footer {
                        text-align: center;
                        margin-top: 2rem;
                        color: #888;
                    }
                    .back-btn {
                        display: inline-block;
                        background: #bb86fc;
                        color: #121212;
                        text-decoration: none;
                        padding: 0.5rem 1rem;
                        border-radius: 4px;
                        margin-top: 1rem;
                    }
                    .back-btn:hover {
                        background: #a370d8;
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="card">
                        <h1>Active Sessions</h1>
                        {% if sessions %}
                        <table>
                            <thead>
                                <tr>
                                    <th>Username</th>
                                    <th>Display Name</th>
                                    <th>Role</th>
                                    <th>Login Time</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for session_id, data in sessions.items() %}
                                <tr>
                                    <td>{{ data.username }}</td>
                                    <td>{{ data.display_name }}</td>
                                    <td>{{ data.role }}</td>
                                    <td>{{ data.login_time|format_time }}</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                        {% else %}
                        <p>No active sessions.</p>
                        {% endif %}
                        <a href="{{ url_for('dashboard') }}" class="back-btn">Back to Dashboard</a>
                    </div>
                </div>
                <div class="footer">
                    made by ShadowDog using AI
                </div>
            </body>
        </html>
    """, sessions=active_sessions, format_time=lambda ts: time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)))

@app.route("/reset_system_prompt", methods=["POST"])
@login_required
def reset_system_prompt():
    # Verify CSRF token
    if request.form.get('csrf_token') != session.get('csrf_token'):
        session['error'] = True
        session['message'] = "Invalid request. Please try again."
        return redirect(url_for('dashboard'))
    
    try:
        # Print current working directory for debugging
        current_dir = os.getcwd()
        print(f"Current working directory: {current_dir}")
        
        # Get absolute paths for better debugging
        default_prompt_path = os.path.abspath(DEFAULT_SYSTEM_PROMPT_FILE)
        system_prompt_path = os.path.abspath(SYSTEM_PROMPT_FILE)
        
        print(f"Looking for default system prompt at: {default_prompt_path}")
        print(f"Will write to active system prompt at: {system_prompt_path}")
        
        # Check if default system prompt file exists
        if not os.path.exists(default_prompt_path):
            # Try looking in the same directory as this script
            script_dir = os.path.dirname(os.path.abspath(__file__))
            alternative_path = os.path.join(script_dir, os.path.basename(DEFAULT_SYSTEM_PROMPT_FILE))
            print(f"File not found. Trying alternative path: {alternative_path}")
            
            if os.path.exists(alternative_path):
                default_prompt_path = alternative_path
                print(f"Found file at alternative path: {default_prompt_path}")
            else:
                session['error'] = True
                session['message'] = f"Default system prompt file not found at {default_prompt_path} or {alternative_path}"
                return redirect(url_for('dashboard'))
            
        # Read the default system prompt
        with open(default_prompt_path, "r", encoding="utf-8") as f:
            default_prompt = f.read()
            print(f"Successfully read default prompt: {len(default_prompt)} chars")
            
        # Write it to the active system prompt file
        with open(system_prompt_path, "w", encoding="utf-8") as f:
            f.write(default_prompt)
            print(f"Successfully wrote to system prompt file")
        
        # Set a success message
        session['message'] = "System prompt reset to default. The server will restart momentarily."
        
        # Create a daemon thread that will wait before restarting
        restart_thread = threading.Thread(target=restart_server, daemon=True)
        restart_thread.start()
        
        # Redirect to dashboard to show the updated prompt
        return redirect(url_for('dashboard'))
    except Exception as e:
        import traceback
        traceback_str = traceback.format_exc()
        print(f"Error resetting system prompt: {str(e)}")
        print(f"Traceback: {traceback_str}")
        session['error'] = True
        session['message'] = f"Error resetting system prompt: {str(e)}"
        return redirect(url_for('dashboard'))

if __name__ == "__main__":
    ws_thread = threading.Thread(target=connect_to_twitch_ws, daemon=True)
    ws_thread.start()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    run_twitch_bot()