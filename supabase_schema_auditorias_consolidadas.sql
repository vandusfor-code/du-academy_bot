create table auditorias_consolidadas (
  id bigint generated always as identity primary key,
  id_corte text unique not null,
  numero text not null,
  nombre_asesora text not null,
  usuario text,
  fecha_auditoria text,
  cantidad_auditorias int,
  nota int,
  hallazgos text,
  puntos_mejora text,
  compromiso text,
  fecha_compromiso timestamptz,
  link_pdf_inicial text,
  link_pdf_final text,
  estado text not null default 'PENDIENTE_ACEPTACION',
  fecha_envio timestamptz not null default now(),
  fecha_lectura timestamptz,
  fecha_cierre timestamptz
);
create index idx_auditorias_consolidadas_numero_estado on auditorias_consolidadas (numero, estado);
