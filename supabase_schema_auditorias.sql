create table auditorias (
  id bigint generated always as identity primary key,
  numero text not null,
  nombre_asesora text not null,
  hallazgos text,
  puntos_mejora text,
  nota int not null,
  estado text not null default 'Enviada',
  compromiso text,
  fecha_compromiso timestamptz,
  message_id text,
  created_at timestamptz not null default now()
);
create index idx_auditorias_numero_estado on auditorias (numero, estado);
