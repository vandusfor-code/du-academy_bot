alter table asesoras add column usuario text;
alter table asesoras add column contrasena text;

create table metricas_pendientes (
  numero text primary key,
  created_at timestamptz not null default now()
);

create table metricas_asesoras (
  id bigint generated always as identity primary key,
  usuario text not null,
  metrica text not null,
  valor text not null,
  fecha text,
  updated_at timestamptz not null default now(),
  unique(usuario, metrica)
);
create index idx_metricas_usuario on metricas_asesoras (usuario);
