import json

with open('snapshot.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

chat_messages = data.get('chat_messages', [])

turns_summary = []

for i in range(0, len(chat_messages), 2):
    user_msg = chat_messages[i] if i < len(chat_messages) else None
    asst_msg = chat_messages[i+1] if i+1 < len(chat_messages) else None
    
    u_text = ""
    if user_msg:
        for c in user_msg.get('content', []):
            if isinstance(c, dict) and c.get('type') == 'text':
                u_text += c.get('text', '')
            elif isinstance(c, str):
                u_text += c
    
    a_text = ""
    tools = []
    if asst_msg:
        for c in asst_msg.get('content', []):
            if isinstance(c, dict):
                if c.get('type') == 'text':
                    a_text += c.get('text', '') + "\n"
                elif c.get('type') == 'tool_use':
                    tools.append(c.get('name'))
            elif isinstance(c, str):
                a_text += c + "\n"
    
    turn_num = i // 2 + 1
    turns_summary.append({
        'turn': turn_num,
        'user_prompt': u_text.strip(),
        'assistant_text': a_text.strip(),
        'tools': tools
    })

print(f"Total turns: {len(turns_summary)}")

# Let's save a detailed markdown file
with open('conversation_full_analysis.md', 'w', encoding='utf-8') as f_out:
    f_out.write(f"# Analysis of Claude Conversation: {data.get('snapshot_name')}\n\n")
    f_out.write(f"- **Snapshot UUID**: {data.get('uuid')}\n")
    f_out.write(f"- **Created At**: {data.get('created_at')}\n")
    f_out.write(f"- **Total Messages**: {len(chat_messages)} ({len(turns_summary)} turns)\n\n")
    
    for t in turns_summary:
        f_out.write(f"## Turn {t['turn']}\n\n")
        f_out.write(f"### User\n```\n{t['user_prompt'][:1000]}\n```\n\n")
        f_out.write(f"### Assistant (Tools: {', '.join(t['tools']) if t['tools'] else 'None'})\n\n")
        # write key paragraphs
        paras = t['assistant_text'].split('\n\n')
        for p in paras:
            f_out.write(f"{p}\n\n")
        f_out.write("---\n\n")

print("Wrote conversation_full_analysis.md")

