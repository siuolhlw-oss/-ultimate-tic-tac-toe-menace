import { integer, sqliteTable, text, index } from 'drizzle-orm/sqlite-core';
export const feedback = sqliteTable('feedback', {
  id: text('id').primaryKey(),
  datasetId: text('dataset_id').notNull(),
  gameIndex: integer('game_index'),
  ply: integer('ply'),
  pathJson: text('path_json').notNull(),
  kind: text('kind').notNull(),
  observation: text('observation').notNull(),
  suggestion: text('suggestion').notNull(),
  createdAt: text('created_at').notNull(),
}, (table) => [index('feedback_created_idx').on(table.createdAt)]);
