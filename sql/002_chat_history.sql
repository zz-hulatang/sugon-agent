-- 应用也会自动建表；消息正文使用 SESSION_ENCRYPTION_KEY 加密。
CREATE TABLE IF NOT EXISTS oa_chat_conversations (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    username VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    title VARCHAR(120) NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    deleted_at DOUBLE NULL,
    INDEX idx_chat_owner_updated (username, deleted_at, updated_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS oa_chat_messages (
    id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    conversation_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    sequence_number BIGINT NOT NULL,
    role VARCHAR(16) NOT NULL,
    payload LONGBLOB NOT NULL,
    created_at DOUBLE NOT NULL,
    UNIQUE KEY idx_chat_message_order (conversation_id, sequence_number),
    CONSTRAINT fk_chat_conversation FOREIGN KEY (conversation_id) REFERENCES oa_chat_conversations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
