-- ============================================================
-- Du Academy Bot - Esquema Supabase
-- ============================================================

create table asesoras (
  id bigint generated always as identity primary key,
  nombre text not null,
  numero text not null,
  numero_alt text,
  cargo text,
  created_at timestamptz not null default now()
);
create index idx_asesoras_numero on asesoras (numero);
create index idx_asesoras_numero_alt on asesoras (numero_alt);

create table registro_pendiente (
  numero text primary key,
  created_at timestamptz not null default now()
);

create table historial_conversaciones (
  id bigint generated always as identity primary key,
  numero text not null,
  rol text not null,
  texto text not null,
  created_at timestamptz not null default now()
);
create index idx_historial_numero_fecha on historial_conversaciones (numero, created_at);

create table mensajes_procesados (
  msg_id text primary key,
  created_at timestamptz not null default now()
);

create table manuales_gemini (
  id bigint generated always as identity primary key,
  id_gemini text not null,
  nombre_archivo text not null,
  created_at timestamptz not null default now()
);
