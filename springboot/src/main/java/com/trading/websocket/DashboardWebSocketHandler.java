package com.trading.websocket;

import org.springframework.stereotype.Component;
import org.springframework.web.socket.*;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.io.IOException;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArraySet;

/**
 * Maintains connected browser sessions and broadcasts JSON state updates.
 */
@Component
public class DashboardWebSocketHandler extends TextWebSocketHandler {

    private final Set<WebSocketSession> sessions = new CopyOnWriteArraySet<>();

    @Override
    public void afterConnectionEstablished(WebSocketSession session) {
        sessions.add(session);
        System.out.println("Browser connected: " + session.getId() + " (total=" + sessions.size() + ")");
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) {
        sessions.remove(session);
        System.out.println("Browser disconnected: " + session.getId());
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) {
        // Browser can send interval change: {"type":"SET_INTERVAL","interval":"5m"}
        String payload = message.getPayload();
        try {
            com.fasterxml.jackson.databind.JsonNode node =
                new com.fasterxml.jackson.databind.ObjectMapper().readTree(payload);
            if ("SET_INTERVAL".equals(node.has("type") ? node.get("type").asText() : "")) {
                String interval = node.get("interval").asText();
                com.trading.model.AppState.get().selectedInterval = interval;
                System.out.println("Interval changed to: " + interval);
            }
        } catch (Exception ignored) {}
    }

    public void broadcast(String json) {
        TextMessage msg = new TextMessage(json);
        for (WebSocketSession session : sessions) {
            if (session.isOpen()) {
                try { session.sendMessage(msg); }
                catch (IOException e) { sessions.remove(session); }
            }
        }
    }

    public int getConnectedCount() { return sessions.size(); }
}
