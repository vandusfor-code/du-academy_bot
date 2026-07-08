create table metricas_solicitudes (
  id bigint generated always as identity primary key,
  numero text not null,
  usuario text,
  created_at timestamptz not null default now()
);
create index idx_metricas_solicitudes_numero on metricas_solicitudes (numero);
