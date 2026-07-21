const { IgApiClient, MQTT } = require('instagram-private-api');
const TelegramBot = require('node-telegram-bot-api');
const fs = require('fs');
const path = require('path');

// ---------- CONFIG ----------
const IG_USERNAME = process.env.IG_USERNAME;
const IG_PASSWORD = process.env.IG_PASSWORD;
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;
// ---------------------------

const SESSION_FILE = path.join(__dirname, 'session.json');

async function loadSession() {
  try {
    if (fs.existsSync(SESSION_FILE)) {
      const data = fs.readFileSync(SESSION_FILE, 'utf8');
      return JSON.parse(data);
    }
  } catch (err) {}
  return null;
}

async function saveSession(session) {
  fs.writeFileSync(SESSION_FILE, JSON.stringify(session, null, 2));
}

const ig = new IgApiClient();
ig.state.generateDevice(IG_USERNAME);

const tg = new TelegramBot(TELEGRAM_TOKEN, { polling: true });

const userThreads = {};
const userMessages = {};

async function loginInstagram() {
  const savedSession = await loadSession();
  if (savedSession) {
    try {
      await ig.state.deserialize(savedSession);
      console.log('✅ Session loaded.');
      return;
    } catch (err) {
      console.log('⚠️ Session expired. Logging in fresh...');
    }
  }
  await ig.account.login(IG_USERNAME, IG_PASSWORD);
  await saveSession(await ig.state.serialize());
  console.log('✅ Logged into Instagram');
}

async function getThreadId(username) {
  if (userThreads[username]) return userThreads[username];
  try {
    const userId = await ig.user.getIdByUsername(username);
    const inbox = await ig.feed.directInbox().items();
    const thread = inbox.find(t => t.users.some(u => u.username === username));
    if (thread) {
      userThreads[username] = thread.thread_id;
      return thread.thread_id;
    }
    return null;
  } catch (err) {
    return null;
  }
}

tg.onText(/\/dm\s+@?(\w+)\s+(.+)/, async (msg, match) => {
  const username = match[1];
  const message = match[2];
  
  try {
    const userId = await ig.user.getIdByUsername(username);
    const threadId = await getThreadId(username);
    if (threadId) {
      await ig.directThread.broadcastText({ threadId, text: message, userIds: [userId] });
    } else {
      const thread = await ig.directThread.broadcastText({ text: message, userIds: [userId] });
      userThreads[username] = thread.thread_id;
    }
    await tg.sendMessage(TELEGRAM_CHAT_ID, `✅ Sent to @${username}`);
  } catch (err) {
    await tg.sendMessage(TELEGRAM_CHAT_ID, `❌ Error: ${err.message}`);
  }
});

tg.onText(/\/reply\s+(\w+)(\d+)\s+(.+)/, async (msg, match) => {
  const username = match[1];
  const msgNumber = parseInt(match[2]) - 1;
  const message = match[3];
  
  try {
    const threadId = await getThreadId(username);
    if (!threadId) {
      await tg.sendMessage(TELEGRAM_CHAT_ID, `❌ No thread found for @${username}`);
      return;
    }
    const msgs = userMessages[username] || [];
    if (msgNumber >= msgs.length || msgNumber < 0) {
      await tg.sendMessage(TELEGRAM_CHAT_ID, `❌ Message ${match[2]} not found. Only ${msgs.length} messages available.`);
      return;
    }
    const targetMsgId = msgs[msgNumber];
    const userId = await ig.user.getIdByUsername(username);
    await ig.directThread.broadcastText({
      threadId,
      text: message,
      userIds: [userId],
      replyToMessageId: targetMsgId
    });
    await tg.sendMessage(TELEGRAM_CHAT_ID, `✅ Reply sent to @${username} (msg ${match[2]})`);
  } catch (err) {
    await tg.sendMessage(TELEGRAM_CHAT_ID, `❌ Error: ${err.message}`);
  }
});

tg.onText(/\/forward\s+@?(\w+)\s+@?(\w+)\s+(.+)/, async (msg, match) => {
  const fromUser = match[1];
  const toUser = match[2];
  const message = match[3];
  
  try {
    const toUserId = await ig.user.getIdByUsername(toUser);
    const threadId = await getThreadId(toUser);
    
    const fullMessage = `📨 *Forwarded from @${fromUser}*: ${message}`;
    if (threadId) {
      await ig.directThread.broadcastText({ threadId, text: fullMessage, userIds: [toUserId] });
    } else {
      const thread = await ig.directThread.broadcastText({ text: fullMessage, userIds: [toUserId] });
      userThreads[toUser] = thread.thread_id;
    }
    await tg.sendMessage(TELEGRAM_CHAT_ID, `✅ Forwarded from @${fromUser} to @${toUser}`);
  } catch (err) {
    await tg.sendMessage(TELEGRAM_CHAT_ID, `❌ Error: ${err.message}`);
  }
});

async function listenDMs() {
  try {
    const mqtt = new MQTT(ig);
    await mqtt.connect();

    mqtt.on('message', async (message) => {
      if (message.type === 'direct_message') {
        const sender = message.sender_username;
        const text = message.text || "📎 Media message";
        const msgId = message.item_id;
        const threadId = message.thread_id;
        
        userThreads[sender] = threadId;
        if (!userMessages[sender]) userMessages[sender] = [];
        userMessages[sender].push(msgId);
        if (userMessages[sender].length > 5) userMessages[sender].shift();

        let replyOptions = `💬 /dm @${sender} <message>\n`;
        for (let i = 0; i < userMessages[sender].length; i++) {
          const num = i + 1;
          replyOptions += `   /reply ${sender}${num} <message>\n`;
        }

        await tg.sendMessage(
          TELEGRAM_CHAT_ID,
          `📨 *${sender}*: ${text}\n\n${replyOptions}`
        );
      }
    });
  } catch (err) {
    console.log('⚠️ MQTT error:', err.message);
    setTimeout(listenDMs, 30000);
  }
}

async function start() {
  console.log('🚀 Starting bot...');
  await loginInstagram();
  await listenDMs();
  console.log('✅ Bot is running!');
}

start();
