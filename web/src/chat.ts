import type { ChatConversationDetail, ChatMessageItem, ChatOut } from "./types";

export interface ChatTurn {
  id: string;
  role: "user" | "assistant";
  text: string;
  meta?: ChatOut;
  streaming?: boolean;
}

export function conversationTitle(title: string): string {
  return title.trim() || "新对话";
}

export function messageMeta(message: ChatMessageItem): ChatOut | null {
  if (message.role !== "assistant" || !message.intent) return null;
  return {
    reply: message.content,
    intent: message.intent,
    skill_id: message.skill_id,
    run_id: message.run_id,
    data: message.data,
    missing: message.missing,
  };
}

export function turnsFromDetail(detail: ChatConversationDetail): ChatTurn[] {
  return detail.messages.map((message) => {
    const meta = messageMeta(message);
    return {
      id: message.id,
      role: message.role,
      text: message.content,
      ...(meta ? { meta } : {}),
    };
  });
}
