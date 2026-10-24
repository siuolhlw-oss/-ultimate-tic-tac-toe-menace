CREATE TABLE `feedback` (
	`id` text PRIMARY KEY NOT NULL,
	`dataset_id` text NOT NULL,
	`game_index` integer,
	`ply` integer,
	`path_json` text NOT NULL,
	`kind` text NOT NULL,
	`observation` text NOT NULL,
	`suggestion` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `feedback_created_idx` ON `feedback` (`created_at`);