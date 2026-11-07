-- 在已创建的 oa_agent 数据库中执行；应用也会自动创建此表。
CREATE TABLE IF NOT EXISTS oa_auth_sessions (
    username VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
    payload LONGBLOB NOT NULL,
    revision CHAR(32) NOT NULL,
    captured_at DOUBLE NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
