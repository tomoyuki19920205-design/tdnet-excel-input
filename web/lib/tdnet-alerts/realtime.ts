"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { createClient } from "@/lib/supabase/client";
import type { TdnetEvent } from "./types";
import { audioManager } from "./audio";
import type { RealtimeChannel } from "@supabase/supabase-js";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

interface UseRealtimeAlertsOptions {
  onNewEvent?: (event: TdnetEvent) => void;
}

export function useRealtimeAlerts(opts: UseRealtimeAlertsOptions = {}) {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const channelRef = useRef<RealtimeChannel | null>(null);
  const onNewEventRef = useRef(opts.onNewEvent);
  onNewEventRef.current = opts.onNewEvent;

  const subscribe = useCallback(() => {
    const supabase = createClient();
    setStatus("connecting");

    const channel = supabase
      .channel("tdnet_events_realtime")
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "tdnet_events",
        },
        (payload) => {
          const newEvent = payload.new as TdnetEvent;
          onNewEventRef.current?.(newEvent);
          // 音通知
          audioManager.playNotification(newEvent.id);
        }
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          setStatus("connected");
        } else if (status === "CLOSED" || status === "CHANNEL_ERROR") {
          setStatus("disconnected");
        }
      });

    channelRef.current = channel;
  }, []);

  useEffect(() => {
    subscribe();

    return () => {
      if (channelRef.current) {
        const supabase = createClient();
        supabase.removeChannel(channelRef.current);
        channelRef.current = null;
      }
      setStatus("disconnected");
    };
  }, [subscribe]);

  return { status };
}
