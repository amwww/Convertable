import path from 'node:path';

/**
 * electron-builder afterSign hook.
 *
 * Requires:
 * - APPLE_ID (your Apple ID email)
 * - APPLE_APP_SPECIFIC_PASSWORD
 * - APPLE_TEAM_ID
 */
export default async function notarizeApp(context) {
	if (process.platform !== 'darwin') return;

	// Only notarize when building for distribution (packaged).
	if (!context?.appOutDir || !context?.packager) return;

	const appleId = process.env.APPLE_ID;
	const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
	const teamId = process.env.APPLE_TEAM_ID;

	if (!appleId || !appleIdPassword || !teamId) {
		console.log('[notarize] Skipping: APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID not set');
		return;
	}

	let notarize;
	try {
		({ notarize } = await import('@electron/notarize'));
	} catch (err) {
		console.warn('[notarize] Skipping: @electron/notarize not installed', err);
		return;
	}

	const appName = context.packager.appInfo.productFilename;
	const appBundleId = context.packager.appInfo.id;
	const appPath = path.join(context.appOutDir, `${appName}.app`);

	console.log(`[notarize] Notarizing ${appPath}`);
	await notarize({
		appBundleId,
		appPath,
		appleId,
		appleIdPassword,
		teamId,
	});
}
