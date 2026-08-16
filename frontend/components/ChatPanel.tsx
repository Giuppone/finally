"use client";

import { useEffect, useRef, useState } from "react";

import type { ChatAction, ChatMessage } from "@/lib/types";

const ACTION_TONE: Record<ChatAction["status"], string> = {
  executed: "border-up/40 bg-up/5 text-up",
  rejected: "border-down/40 bg-down/5 text-down",
  skipped: "border-edge bg-raised text-muted",
};

const ACTION_MARK: Record<ChatAction["status"], string> = {
  executed: "✓",
  rejected: "✕",
  skipped: "–",
};

function ActionChip({ action }: { action: ChatAction }) {
  return (
    <div
      data-testid={`chat-action-${action.status}`}
      className={`flex gap-2 border px-2 py-1 text-2xs ${ACTION_TONE[action.status]}`}
    >
      <span aria-hidden>{ACTION_MARK[action.status]}</span>
      <span className="tnum">{action.detail}</span>
    </div>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex flex-col gap-1.5 ${isUser ? "items-end" : "items-start"}`}>
      <div
        data-testid={`chat-message-${message.role}`}
        className={`max-w-[92%] whitespace-pre-wrap px-2.5 py-1.5 text-xs leading-relaxed ${
          isUser
            ? "border border-brand/30 bg-brand/10 text-ink"
            : "border border-edge bg-raised text-ink"
        }`}
      >
        {message.content}
      </div>
      {message.actions.length > 0 && (
        <div className="flex w-full flex-col gap-1">
          {message.actions.map((action, index) => (
            <ActionChip key={index} action={action} />
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPanel({
  messages,
  pending,
  sending,
  error,
  mock,
  onSend,
}: {
  messages: ChatMessage[];
  /** The user's message, echoed immediately so the panel never feels stalled. */
  pending: string | null;
  sending: boolean;
  error: string | null;
  mock: boolean;
  onSend: (text: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Stick to the bottom as the conversation grows.
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [messages, pending, sending]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text || sending) return;
    onSend(text);
    setDraft("");
  };

  return (
    <section className="panel flex min-h-0 flex-col">
      <div className="panel-title">
        <span>FinAlly Assistant</span>
        {mock && (
          <span
            title="LLM_MOCK=true — replies are deterministic and no model is called."
            className="border border-accent/50 px-1.5 py-0.5 text-2xs text-accent"
          >
            MOCK
          </span>
        )}
      </div>

      <div
        ref={scroller}
        data-testid="chat-log"
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-2.5"
      >
        {messages.length === 0 && !pending && (
          <div className="m-auto max-w-[16rem] text-center text-xs leading-relaxed text-faint">
            Ask about your portfolio, request analysis, or tell me to trade.
            <span className="mt-2 block text-2xs">
              &ldquo;How concentrated am I?&rdquo; · &ldquo;Buy 10 MU&rdquo; · &ldquo;Watch
              NVDA&rdquo;
            </span>
          </div>
        )}

        {messages.map((message) => (
          <Bubble key={message.id} message={message} />
        ))}

        {pending && (
          <Bubble
            message={{
              id: "pending",
              role: "user",
              content: pending,
              actions: [],
              created_at: "",
            }}
          />
        )}

        {sending && (
          <div className="flex items-center gap-1.5 px-1 text-2xs text-muted">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
            analysing…
          </div>
        )}

        {error && (
          <div className="border border-down/40 bg-down/5 px-2 py-1.5 text-2xs text-down">
            {error}
          </div>
        )}
      </div>

      <form onSubmit={submit} className="border-t border-edge p-2">
        <div className="flex gap-1.5">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask FinAlly…"
            aria-label="Message FinAlly"
            disabled={sending}
            data-testid="chat-input"
            className="field w-full text-xs"
          />
          <button
            type="submit"
            disabled={sending || !draft.trim()}
            data-testid="chat-send"
            className="btn btn-submit"
          >
            Send
          </button>
        </div>
      </form>
    </section>
  );
}
