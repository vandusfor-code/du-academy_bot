alter table asesoras add column area text;

create table pildoras_enviadas (
  id bigint generated always as identity primary key,
  fecha date not null default current_date,
  area text not null,
  categoria text not null,
  pildora text not null,
  total_enviadas int not null default 0,
  aplicaran int not null default 0,
  created_at timestamptz not null default now()
);

create table pildoras_pendientes (
  numero text primary key,
  pildora text not null,
  categoria text not null,
  area text not null,
  created_at timestamptz not null default now()
);
