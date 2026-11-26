Endpoints:
Path	Who
POST	/orgs	any signed-in user (becomes admin)
GET	/orgs	your organizations with your role
GET, PATCH, DELETE	/orgs/{org_id}	read: any member. Rename and delete: admin
GET	/orgs/{org_id}/members	any member
POST	/orgs/{org_id}/members	admin, add by email with a role
PATCH	/orgs/{org_id}/members/{user_id}	admin, change role
DELETE	/orgs/{org_id}/members/{user_id}	admin removes anyone. Any member can leave

