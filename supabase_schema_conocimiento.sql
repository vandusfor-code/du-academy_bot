create table conocimiento_extra (
  id bigint generated always as identity primary key,
  contenido text not null,
  created_at timestamptz not null default now()
);
