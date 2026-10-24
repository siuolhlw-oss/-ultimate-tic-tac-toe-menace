import { env } from 'cloudflare:workers';
import { getChatGPTUser } from '../app/chatgpt-auth';
import { teacherAllowed } from './feedback.mjs';
export async function isTeacher() { return teacherAllowed(await getChatGPTUser(), env.TEACHER_EMAIL); }
